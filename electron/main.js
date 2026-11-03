'use strict';
/**
 * Electron 主进程。
 *
 * 两件事最容易出错，这里都显式处理：
 *
 * 1) 「所有用户文件都在安装目录下」
 *    Electron / Chromium 默认会把 userData、会话数据、崩溃转储写到
 *    %APPDATA% / %TEMP%（也就是用户目录）。必须在 app ready 之前
 *    用 app.setPath(...) 改道，否则会先创建再改，已经晚了。
 *    本程序把这些全部指向 <安装目录>/runtime。
 *
 * 2) Python 边车进程的生命周期
 *    WeChat 自动化靠 Python（wechatauto-replica + uiautomation），
 *    Electron 负责拉起、读端口、退出时回收。端口用 0 让系统分配，
 *    避免固定端口被占用。
 */

const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn } = require('child_process');

const IS_DEV = !app.isPackaged;

// ---------------------------------------------------------------------------
// 1. 安装目录与用户文件根目录（必须在 ready 之前）
// ---------------------------------------------------------------------------
const APP_DIR = IS_DEV
  ? path.resolve(__dirname, '..')
  : path.dirname(app.getPath('exe'));

const DATA_ROOT = APP_DIR;

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
  return p;
}

function isWritable(dir) {
  try {
    ensureDir(dir);
    const probe = path.join(dir, '.write-test');
    fs.writeFileSync(probe, 'ok');
    fs.unlinkSync(probe);
    return true;
  } catch (e) {
    return false;
  }
}

const DIRS = {
  data: path.join(DATA_ROOT, 'data'),
  logs: path.join(DATA_ROOT, 'logs'),
  shots: path.join(DATA_ROOT, 'shots'),
  cache: path.join(DATA_ROOT, 'cache'),
  tmp: path.join(DATA_ROOT, 'tmp'),
  runtime: path.join(DATA_ROOT, 'runtime'),
  electron: path.join(DATA_ROOT, 'runtime', 'electron'),
  session: path.join(DATA_ROOT, 'runtime', 'session'),
  crash: path.join(DATA_ROOT, 'runtime', 'crash'),
};

// 把 Electron / Chromium 的所有写入改到安装目录下
for (const d of Object.values(DIRS)) ensureDir(d);

app.setPath('userData', DIRS.electron);
app.setPath('sessionData', DIRS.session);
app.setPath('temp', DIRS.tmp);
app.setPath('crashDumps', DIRS.crash);
app.setPath('logs', DIRS.logs);

// 关键补充：app.setPath('temp') 只改浏览器进程的 PathService，
// Chromium 的**子进程**（renderer / GPU / network）是用继承来的 TMP/TEMP
// 环境变量经 GetTempPath() 自己解析临时目录的。不覆盖环境变量的话，
// 它们仍会在真实的 %TEMP%（即用户目录下）创建文件 —— 实测每次启动会
// 留下一个 0 字节的 GUID .tmp。所以这里必须同时改自身进程的环境变量，
// 子进程才会继承到安装目录内。
process.env.TEMP = DIRS.tmp;
process.env.TMP = DIRS.tmp;
process.env.TMPDIR = DIRS.tmp;

// 关掉不需要的联网/遥测组件，避免额外的用户目录写入
app.commandLine.appendSwitch('disable-features', 'MediaRouter');
app.commandLine.appendSwitch('disable-crash-reporter');

// ---------------------------------------------------------------------------
// 2. Python 边车
// ---------------------------------------------------------------------------
const TOKEN = crypto.randomBytes(24).toString('hex');

function pythonCandidates() {
  const list = [];
  if (app.isPackaged) {
    list.push({
      cmd: path.join(process.resourcesPath, 'python', 'python.exe'),
      args: [],
      bundled: true,
    });
  }
  list.push({ cmd: 'python', args: [], bundled: false });
  list.push({ cmd: 'py', args: ['-3'], bundled: false });
  return list;
}

function spawnPython(cand) {
  const serverPath = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'server.py')
    : path.join(__dirname, '..', 'backend', 'server.py');

  const env = Object.assign({}, process.env, {
    WCS_DATA_ROOT: DATA_ROOT,
    TEMP: DIRS.tmp,
    TMP: DIRS.tmp,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
  });

  const child = spawn(
    cand.cmd,
    [...cand.args, serverPath, '0', '--token', TOKEN, '--quiet'],
    {
      cwd: DATA_ROOT,
      env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  );
  return child;
}

function waitForPort(child, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    let buf = '';
    let stderrBuf = '';
    const timer = setTimeout(() => {
      reject(new Error('启动超时：Python 服务在 ' + (timeoutMs / 1000) + ' 秒内未就绪。\n' + stderrBuf.slice(-2000)));
    }, timeoutMs);

    child.stdout.on('data', (d) => {
      const text = d.toString('utf8');
      buf += text;
      log('python: ' + text.trimEnd());
      const m = buf.match(/PORT=(\d+)/);
      if (m) {
        clearTimeout(timer);
        resolve(parseInt(m[1], 10));
      }
    });
    child.stderr.on('data', (d) => {
      const text = d.toString('utf8');
      stderrBuf += text;
      log('python(stderr): ' + text.trimEnd());
    });
    child.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error('Python 服务提前退出（退出码 ' + code + '）。\n' + stderrBuf.slice(-2000)));
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

function log(line) {
  const line2 = '[' + new Date().toISOString() + '] ' + line + '\n';
  process.stdout.write(line2);
  try {
    fs.appendFileSync(path.join(DIRS.logs, 'electron.log'), line2, 'utf8');
  } catch (e) {
    /* 忽略 */
  }
}

// ---------------------------------------------------------------------------
// 3. 窗口
// ---------------------------------------------------------------------------
let pyChild = null;
let win = null;

// 素材选择：渲染进程拿不到文件绝对路径（<input type=file> 只给文件名），
// 所以由主进程弹系统对话框，把真实路径回传给界面。
function registerIpc() {
  ipcMain.handle('wcs:pick-files', async () => {
    if (!win) return [];
    try {
      const r = await dialog.showOpenDialog(win, {
        title: '选择要发送的图片或文件',
        properties: ['openFile', 'multiSelections'],
        filters: [
          { name: '图片', extensions: ['png', 'jpg', 'jpeg', 'bmp', 'gif', 'webp'] },
          { name: '文档与压缩包', extensions: ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
                                                'txt', 'md', 'csv', 'json', 'zip', 'rar', '7z'] },
          { name: '全部文件', extensions: ['*'] },
        ],
      });
      if (r.canceled) return [];
      return r.filePaths || [];
    } catch (e) {
      return [];
    }
  });

  // 开机自启：用 Electron 原生 API（底层写 HKCU 的 Run 键），
  // 比自己在注册表里拼字符串可靠 —— 安装路径含 "&" 等字符时也不会写坏。
  // 开发模式（electron .）不写：那会把 electron.exe 注册成启动项，没有意义。
  ipcMain.handle('wcs:get-autostart', () => {
    try {
      if (!app.isPackaged) {
        return { ok: true, on: false, packaged: false };
      }
      const s = app.getLoginItemSettings();
      return { ok: true, on: !!s.openAtLogin, packaged: true,
               exe: process.execPath };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  ipcMain.handle('wcs:set-autostart', (e, on) => {
    try {
      if (!app.isPackaged) {
        return { ok: false, packaged: false,
                 error: '开发模式下不写开机自启（会注册成 electron.exe）。'
                      + '装好安装版后再设置。' };
      }
      app.setLoginItemSettings({
        openAtLogin: !!on,
        path: process.execPath,
        args: [],
      });
      const s = app.getLoginItemSettings();
      return { ok: true, on: !!s.openAtLogin };
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err) };
    }
  });
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1280,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: '微信定时群发控制台',
    backgroundColor: '#f7f8fa',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      additionalArguments: [
        '--wcs-port=' + port,
        '--wcs-token=' + TOKEN,
        '--wcs-dataroot=' + DATA_ROOT,
      ],
    },
  });

  const uiPath = app.isPackaged
    ? path.join(process.resourcesPath, 'frontend', 'index.html')
    : path.join(__dirname, '..', 'frontend', 'index.html');

  win.loadFile(uiPath);
  win.on('closed', () => {
    win = null;
  });

  // 把界面加载结果与渲染进程报错写进日志，方便排查（看不到窗口时也能判断）
  win.webContents.on('did-finish-load', () => log('界面已加载: ' + uiPath));
  win.webContents.on('did-fail-load', (_e, code, desc, url) =>
    log('界面加载失败 code=' + code + ' desc=' + desc + ' url=' + url)
  );
  win.webContents.on('console-message', (...args) => {
    // Electron <34: (event, level, message, line, sourceId)
    // Electron >=34: (details)
    if (args.length >= 4 && typeof args[1] === 'number') {
      log('renderer[' + args[1] + '] ' + args[2] + ' @' + args[4] + ':' + args[3]);
    } else if (args[0] && typeof args[0] === 'object') {
      const d = args[0];
      log('renderer[' + d.level + '] ' + d.message + ' @' + d.sourceId + ':' + d.lineNumber);
    }
  });
  win.webContents.on('render-process-gone', (_e, details) =>
    log('渲染进程异常退出: ' + JSON.stringify(details))
  );

  // 外部链接用系统浏览器打开，不在应用内导航
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
}

function stopPython() {
  if (!pyChild || pyChild.killed) return;
  log('正在停止 Python 服务…');
  try {
    pyChild.kill();
  } catch (e) {
    /* 忽略 */
  }
  pyChild = null;
}

// ---------------------------------------------------------------------------
// 4. 启动流程
// ---------------------------------------------------------------------------
async function boot() {
  if (!isWritable(DATA_ROOT)) {
    dialog.showErrorBox(
      '安装目录不可写',
      '本程序要求把用户文件放在安装目录下：\n' + DATA_ROOT +
        '\n\n但该目录当前不可写。\n' +
        '请把程序安装/解压到可写位置，例如 D:\\WeChatSender。\n' +
        '（不要装在 C:\\Program Files 下，除非以管理员身份运行。）'
    );
    app.quit();
    return;
  }

  log('安装目录: ' + APP_DIR);
  log('用户文件根目录: ' + DATA_ROOT);

  let lastErr = null;
  for (const cand of pythonCandidates()) {
    try {
      log('尝试 Python: ' + cand.cmd + ' ' + cand.args.join(' '));
      const child = spawnPython(cand);
      const port = await waitForPort(child);
      pyChild = child;
      log('Python 服务已就绪，端口 ' + port);

      child.on('exit', (code) => {
        log('Python 服务退出，退出码 ' + code);
        pyChild = null;
      });

      createWindow(port);
      return;    } catch (err) {
      lastErr = err;
      log('该 Python 不可用: ' + err.message);
    }
  }

  dialog.showErrorBox(
    '无法启动后端服务',
    '没有找到可用的 Python 解释器，或服务启动失败。\n\n' +
      '请先安装 Python 3.9+ 并加入 PATH：\n' +
      '  https://www.python.org/downloads/\n\n' +
      '错误详情：\n' +
      (lastErr ? lastErr.message : '未知错误')
  );
  app.quit();
}

// Windows 上弹系统通知必须先设 AppUserModelId，否则 new Notification() 会
// **静默失败**（不报错、也不弹窗），非常难查。取值与 package.json 的
// build.appId 保持一致。
app.setAppUserModelId('com.alanine.wechat.sender');

app.whenReady().then(() => {
  registerIpc();
  boot();
});

app.on('window-all-closed', () => {
  stopPython();
  app.quit();
});

app.on('before-quit', stopPython);
process.on('exit', stopPython);