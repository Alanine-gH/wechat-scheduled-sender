'use strict';
/**
 * electron-builder 启动器（打包用）。
 *
 * 解决两个问题：
 *
 * 1) 路径含 `&` 会打断 npm 生成的 .bin shim
 *    `npm run dist` 实际执行的是 node_modules/.bin/electron-builder.cmd，
 *    里面的路径没有正确加引号；路径含 `&` 时 cmd.exe 把它当命令分隔符，报：
 *        '...\node_modules\.bin\' is not recognized as an internal or external command
 *        Error: Cannot find module 'D:\静静\electron-builder\cli.js'
 *    这里直接定位 node_modules/electron-builder/cli.js 并用 spawn（不走 shell）拉起，
 *    与 launch.js 处理 electron 的方式一致。
 *
 * 2) electron-builder 默认把构建缓存写到 %LOCALAPPDATA%\electron-builder
 *    本项目的约定是**所有文件都在安装目录下**，所以用 ELECTRON_BUILDER_CACHE
 *    把它钉到 <项目>\cache\electron-builder；同时把 Electron 二进制缓存也钉住。
 *    （这两个缓存是构建期产物，不随程序分发，但同样不该污染用户目录。）
 *
 * 3) 自有 Python 代码不随包分发明文
 *    打包前把 backend/*.py 编译加密成 temp/protected-backend/ 下的
 *    core.bin + sourceless _boot.pyc，package.json 的 extraResources 指向它。
 *    必须用 vendor/python/python.exe 跑（marshal 字节码与 3.14.7 绑定，
 *    系统 Python 版本不同会产出无法加载的 .pyc）。
 *    构建结束（无论成败）清理该临时目录，不留在工作区。
 *
 * 用法：
 *    node build.js --win --x64                NSIS 安装包
 *    node build.js --win portable --x64       免安装便携版
 */
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const projectDir = __dirname;
const cli = path.join(projectDir, 'node_modules', 'electron-builder', 'cli.js');

if (!fs.existsSync(cli)) {
  process.stderr.write(
    '\n[build] 找不到 electron-builder（' + cli + '）。\n' +
      '        请先在本目录执行：npm install\n\n'
  );
  process.exit(1);
}

const cacheRoot = path.join(projectDir, 'cache');

const env = Object.assign({}, process.env);
delete env.ELECTRON_RUN_AS_NODE;              // 见 launch.js 里的说明
env.ELECTRON_BUILDER_CACHE = path.join(cacheRoot, 'electron-builder');
env.ELECTRON_CACHE = path.join(cacheRoot, 'electron');

// electron-builder 还需要下载 nsis / winCodeSign 等二进制，默认走 GitHub，
// 国内经常连不上（表现为卡住或 ERR_ELECTRON_BUILDER_CANNOT_EXECUTE）。
// 这里默认改走 npmmirror 镜像；想用官方源就设 ELECTRON_BUILDER_BINARIES_MIRROR=""。
if (env.ELECTRON_BUILDER_BINARIES_MIRROR === undefined) {
  env.ELECTRON_BUILDER_BINARIES_MIRROR =
    'https://npmmirror.com/mirrors/electron-builder-binaries/';
}

fs.mkdirSync(env.ELECTRON_BUILDER_CACHE, { recursive: true });
fs.mkdirSync(env.ELECTRON_CACHE, { recursive: true });

const args = process.argv.slice(2);

// ---------------------------------------------------------------------------
// 版本号：支持 node build.js --version 1.2.3 ...
// electron-builder 的产物名和「应用版本」都取自 package.json 的 version，
// 之前只能改 package.json 才能换版本，很容易忘。
// 这里允许命令行指定：构建前临时改 package.json，**构建结束（无论成败）
// 都原样还原**，不把版本号留在文件里。
// ---------------------------------------------------------------------------
const pkgPath = path.join(projectDir, 'package.json');
const pkgRaw = fs.readFileSync(pkgPath, 'utf8');
let version = null;
const rest = [];
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === '--version') {
    version = args[++i];
  } else if (a.startsWith('--version=')) {
    version = a.slice('--version='.length);
  } else {
    rest.push(a);
  }
}

let restorePkg = null;
if (version) {
  if (!/^\d+\.\d+\.\d+/.test(version)) {
    process.stderr.write(
      '\n[build] 版本号格式不对：' + version + '\n' +
        '        应为 1.2.3 这样的形式，例如：node build.js --version 1.2.0\n\n'
    );
    process.exit(1);
  }
  const pkg = JSON.parse(pkgRaw);
  const old = pkg.version;
  pkg.version = version;
  fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + '\n');
  restorePkg = () => fs.writeFileSync(pkgPath, pkgRaw);
  process.stdout.write('[build] 版本号 ' + old + ' -> ' + version + '（构建后自动还原）\n');
}

let restored = false;

// --- 自有 Python 代码保护产物 -------------------------------------------
// 每次构建用随机密钥重新生成，产物只存在于本次构建期间。
const protectedDir = path.join(projectDir, 'temp', 'protected-backend');
const bundledPython = path.join(projectDir, 'vendor', 'python', 'python.exe');
const protectScript = path.join(projectDir, 'tools', 'protect_backend.py');

function cleanupProtected() {
  try { fs.rmSync(protectedDir, { recursive: true, force: true }); }
  catch (e) { /* 忽略 */ }
}

if (!fs.existsSync(bundledPython)) {
  process.stderr.write(
    '\n[build] 缺少内嵌 Python: ' + bundledPython + '\n' +
    '        请先运行 tools/prepare-python.ps1（或「准备内嵌Python.bat」）。\n' +
    '        保护脚本必须用它编译，不能用系统 Python（字节码版本绑定）。\n\n');
  process.exit(1);
}
const pr = spawnSync(bundledPython,
  [protectScript, '--src', path.join(projectDir, 'backend'),
   '--out', protectedDir],
  { cwd: projectDir, encoding: 'utf8', timeout: 120000 });
if (pr.status !== 0) {
  process.stderr.write('[build] 保护 Python 代码失败:\n'
    + (pr.stdout || '') + '\n' + (pr.stderr || '') + '\n');
  cleanupProtected();
  restore();
  process.exit(1);
}
process.stdout.write((pr.stdout || '').trimEnd() + '\n');

function restore() {
  if (restored) return;
  restored = true;
  if (restorePkg) {
    try {
      restorePkg();
    } catch (e) {
      process.stderr.write('[build] 还原 package.json 失败：' + e.message + '\n');
    }
  }
  cleanupProtected();
}
process.on('exit', restore);

// electron-builder 检测到 CI 环境（或带 tag 构建）时会**自己去发布 Release**，
// 需要 GH_TOKEN；没配就报
//   "GitHub Personal Access Token is not set ... using env GH_TOKEN"
// 而构建其实已经成功。本项目的发布统一交给 CI 里的 action（或手动 gh release），
// 所以这里默认补 --publish never，除非调用方自己指定了 --publish。
const hasPublish = rest.some((a) => a === '--publish' || a.startsWith('--publish='));
const finalArgs = hasPublish ? rest : [...rest, '--publish', 'never'];

process.stdout.write('[build] electron-builder 缓存目录: ' + env.ELECTRON_BUILDER_CACHE + '\n');
process.stdout.write('[build] 二进制镜像: ' + (env.ELECTRON_BUILDER_BINARIES_MIRROR || '(官方源)') + '\n');
process.stdout.write('[build] 参数: ' + (finalArgs.length ? finalArgs.join(' ') : '(空，默认当前平台)') + '\n\n');

const child = spawn(process.execPath, [cli, ...finalArgs], {
  stdio: 'inherit',
  cwd: projectDir,
  env,
});

child.on('close', (code) => {
  restore();
  process.exit(code === null ? 0 : code);
});
child.on('error', (err) => {
  restore();
  process.stderr.write('[build] 启动 electron-builder 失败：' + err.message + '\n');
  process.exit(1);
});