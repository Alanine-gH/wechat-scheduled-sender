'use strict';
/**
 * Electron 启动器（避开 npm 的 .bin 批处理 shim，并在启动前确保 Electron 二进制就绪）。
 *
 * 为什么需要它：
 *  1) npm 会在 node_modules/.bin 生成 electron.cmd，里面的路径没有正确加引号。
 *     项目路径若含 `&`（例如 D:\some&dir\project），cmd.exe 会把 `&` 当命令分隔符，
 *     于是 `npm start` 直接报
 *       "'...\node_modules\.bin\' is not recognized as an internal or external command"。
 *     这里改用 child_process.spawn（默认不走 shell），路径交给 Node 处理。
 *
 *  2) npm 11 默认拦截未被批准的 postinstall 脚本，导致 electron 的 install.js 不执行，
 *     node_modules/electron/dist 不存在。此时 `node_modules/electron/package.json` 仍在，
 *     光看它存在性会误判成「已安装」。所以这里真正校验 dist/electron.exe 并就绪修复。
 *
 *  3) 本机环境存在全局 ELECTRON_RUN_AS_NODE=1，会让 electron.exe 退化成纯 Node，
 *     此时 require('electron') 只返回 npm 包的路径字符串、主进程拿不到 app，
 *     表现为 "Cannot read properties of undefined (reading 'isPackaged')"。
 *     必须对子进程环境清掉该变量。
 */
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const projectDir = __dirname;
const extraArgs = process.argv.slice(2);
const pkgDir = path.join(projectDir, 'node_modules', 'electron');
const distPath = path.join(pkgDir, 'dist');
const exePath = path.join(distPath, 'electron.exe');
const pathTxt = path.join(pkgDir, 'path.txt');

function note(msg) {
  process.stdout.write('[launcher] ' + msg + '\n');
}

function fail(msg, detail) {
  process.stderr.write('\n[launcher] ' + msg + '\n');
  if (detail) process.stderr.write(detail + '\n');
  process.stdin.resume();
  process.stdin.once('data', () => process.exit(1));
  setTimeout(() => process.exit(1), 30000);
}

/** 从 electron 包里解析可执行文件路径（读 path.txt，不依赖 require 缓存）。 */
function resolveExe() {
  if (fs.existsSync(pathTxt)) {
    const name = fs.readFileSync(pathTxt, 'utf8').trim();
    if (name) {
      const p = path.join(distPath, name);
      if (fs.existsSync(p)) return p;
    }
  }
  if (fs.existsSync(exePath)) return exePath;
  return null;
}

/** 尝试修复：先批准安装脚本，再跑 install.js（用项目内缓存，不写用户目录）。 */
function tryRepair() {
  const env = Object.assign({}, process.env);
  delete env.ELECTRON_RUN_AS_NODE;
  // 把 Electron 的下载缓存钉在安装目录，避免写 %LOCALAPPDATA%
  env.ELECTRON_CACHE = path.join(projectDir, 'cache', 'electron');

  if (!fs.existsSync(path.join(pkgDir, 'install.js'))) {
    note('找不到 electron/install.js，无法自动修复。');
    return false;
  }

  note('Electron 二进制缺失，尝试修复（npm install-scripts approve electron）…');
  try {
    spawnSync('npm', ['install-scripts', 'approve', 'electron'], {
      cwd: projectDir, env, stdio: 'ignore', shell: true, timeout: 60000,
    });
  } catch (e) {
    /* 老版本 npm 没有这个子命令，忽略 */
  }

  note('运行 electron/install.js（最长 180 秒；它会用安装目录内的下载缓存）…');
  const r = spawnSync(process.execPath, ['install.js'], {
    cwd: pkgDir, env, stdio: 'inherit', timeout: 180000,
  });
  if (r.error && r.error.code === 'ETIMEDOUT') {
    note('install.js 超时（通常是网络校验卡住）。');
  }
  return !!resolveExe();
}

function manualHint() {
  return [
    '',
    '请手动完成 Electron 二进制安装（任选其一）：',
    '',
    '  A) 批准安装脚本后重装：',
    '       npm install-scripts approve electron',
    '       npm install',
    '',
    '  B) 直接手工解压（离线也可）：',
    '       在 cache\\electron 下找到 electron-v*-win32-x64.zip，',
    '       解压到 node_modules\\electron\\dist\\，',
    '       并在 node_modules\\electron\\path.txt 写入一行：electron.exe',
    '',
    '完成后重新双击 启动.bat。',
    '',
  ].join('\n');
}

// ---------------------------------------------------------------------------
// 1. 确保 Electron 二进制就绪
// ---------------------------------------------------------------------------
if (!fs.existsSync(pkgDir)) {
  fail('尚未安装依赖（找不到 node_modules\\electron）。请先在本目录执行：npm install',
       manualHint());
}

let execPath = resolveExe();
if (!execPath) {
  if (tryRepair()) {
    execPath = resolveExe();
  }
}
if (!execPath) {
  fail('Electron 二进制仍不可用。', manualHint());
}
note('Electron 可执行文件: ' + execPath);

// ---------------------------------------------------------------------------
// 2. 启动 Electron
// ---------------------------------------------------------------------------
const childEnv = Object.assign({}, process.env);
delete childEnv.ELECTRON_RUN_AS_NODE;   // 见文件头说明 3)

const child = spawn(execPath, [projectDir, ...extraArgs], {
  stdio: 'inherit',
  windowsHide: false,
  cwd: projectDir,
  env: childEnv,
});

child.on('close', (code) => process.exit(code === null ? 0 : code));
child.on('error', (err) => {
  fail('无法启动 Electron：' + err.message);
});