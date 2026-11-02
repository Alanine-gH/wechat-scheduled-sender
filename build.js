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
 * 用法：
 *    node build.js --win --x64                NSIS 安装包
 *    node build.js --win portable --x64       免安装便携版
 */
const { spawn } = require('node:child_process');
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
process.stdout.write('[build] electron-builder 缓存目录: ' + env.ELECTRON_BUILDER_CACHE + '\n');
process.stdout.write('[build] 二进制镜像: ' + env.ELECTRON_BUILDER_BINARIES_MIRROR + '\n');
process.stdout.write('[build] 参数: ' + (args.length ? args.join(' ') : '(空，默认当前平台)') + '\n\n');

const child = spawn(process.execPath, [cli, ...args], {
  stdio: 'inherit',
  cwd: projectDir,
  env,
});

child.on('close', (code) => process.exit(code === null ? 0 : code));
child.on('error', (err) => {
  process.stderr.write('[build] 启动 electron-builder 失败：' + err.message + '\n');
  process.exit(1);
});