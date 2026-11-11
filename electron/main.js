'use strict';
/**
 * Electron 主进程。
 *
 * 两件事最容易出错，这里都显式处理：
 *
 * 1) 「所有用户文件都不在 ~ 下，也不在安装目录里」
 *    Electron / Chromium 默认会把 userData、会话数据、崩溃转储写到
 *    %APPDATA% / %TEMP%（用户目录）。必须在 app ready 之前
 *    用 app.setPath(...) 改道，否则会先创建再改，已经晚了。
 *    本程序把它们指向 <安装目录同级>/WeChatSender-Data/runtime。
 *    数据根不放在 $INSTDIR 内，是因为 NSIS 覆盖安装/卸载会
 *    `RMDir /r $INSTDIR` 把整个安装目录删光。
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
// 为什么数据根目录不在安装目录里：
//   electron-builder 的 NSIS 卸载段是 `RMDir /r $INSTDIR`（模板
//   node_modules/app-builder-lib/templates/nsis/uninstaller.nsh:169），
//   覆盖安装 / 卸载会把整个安装目录删光。以前 data/ logs/ shots/ ... 都在
//   安装目录里，一升级用户数据就没了；而且正在运行的 WeChatSender.exe 与
//   Python 边车也在 $INSTDIR 内，atomicRMDir 遇到被占用文件会直接 Abort
//   （electron-builder 已知问题 #6865 / #8131 / #9181），表现为"安装卡死"。
//
//   现在数据根放在**安装目录的同级目录**（不在 ~ / %APPDATA% 下），
//   NSIS 只删 $INSTDIR，永远碰不到用户数据，也不在用户主目录里拉屎。
//   下载的安装包同样放数据根下的 updates/，安装包自己也不会被删。
// ---------------------------------------------------------------------------
const APP_DIR = IS_DEV
  ? path.resolve(__dirname, '..')
  : path.dirname(app.getPath('exe'));

const APP_PARENT = path.dirname(APP_DIR);
const DEFAULT_DATA_ROOT = path.join(APP_PARENT, 'WeChatSender-Data');
// 自定义数据根记在同级的引导文件里。它必须在「安装目录之外」，
// 否则升级时同样会被 NSIS 删掉，配置本身也就丢了。
const DATA_ROOT_POINTER = path.join(APP_PARENT, 'WeChatSender-Data.location.json');
const USER_DIR_NAMES = ['data', 'logs', 'shots', 'cache', 'tmp', 'runtime'];

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

function readDataRootPointer() {
  try {
    const v = JSON.parse(fs.readFileSync(DATA_ROOT_POINTER, 'utf8'));
    const p = typeof v === 'string' ? v : (v && v.dataRoot);
    return p ? path.resolve(String(p)) : '';
  } catch (e) {
    return '';
  }
}

function resolveDataRoot() {
  if (IS_DEV) return APP_DIR;               // 开发模式不动，免得污染项目外面
  // 优先级：环境变量 > 引导文件 > 默认同级目录
  const candidates = [
    (process.env.WCS_DATA_ROOT || '').trim(),
    readDataRootPointer(),
    DEFAULT_DATA_ROOT,
  ];
  for (const c of candidates) {
    if (!c) continue;
    const p = path.resolve(c);
    // 不允许指回安装目录（否则升级又会被删）；不可写就换下一个
    if (p === APP_DIR || p.startsWith(APP_DIR + path.sep)) continue;
    if (isWritable(p)) return p;
  }
  return DEFAULT_DATA_ROOT;
}

const DATA_ROOT = resolveDataRoot();
// 供 Python 侧与界面读取（Python 的 userdata.configure 认这个环境变量）
process.env.WCS_DATA_ROOT = DATA_ROOT;

function migrateLegacyData() {
  /** 把旧版本留在安装目录里的用户数据搬到新数据根。
   *  只在目标为空时搬，保证幂等：NSIS customInit 可能已经先抢救过一份。
   *  用户明确要求「移动」而不是复制/删除；跨盘 rename 失败时退化为
   *  复制并**保留源**，绝不静默删用户文件。
   */
  if (IS_DEV || path.resolve(DATA_ROOT) === path.resolve(APP_DIR)) return [];
  const moved = [];
  for (const name of USER_DIR_NAMES) {
    const src = path.join(APP_DIR, name);
    const dst = path.join(DATA_ROOT, name);
    if (!fs.existsSync(src)) continue;
    try {
      if (fs.existsSync(dst) && fs.readdirSync(dst).length > 0) continue;
      fs.mkdirSync(DATA_ROOT, { recursive: true });
      try {
        fs.renameSync(src, dst);           // 同盘：真·移动
        moved.push(name);
      } catch (e) {
        fs.cpSync(src, dst, { recursive: true, force: false });  // 跨盘：复制保底
        moved.push(name + '(复制,源保留)');
      }
    } catch (e) {
      // 此时 logs/ 可能还没创建，log() 的文件写入会失败；直接 stdout 更可靠
      process.stdout.write('[migrate] ' + name + ' 失败: '
        + ((e && e.message) || e) + '\n');
    }
  }
  return moved;
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
  updates: path.join(DATA_ROOT, 'updates'),   // 下载的安装包，绝不放 $INSTDIR 里
};

// 目录创建之前先搬老数据，避免先建出空目录导致迁移被跳过
const MIGRATED = migrateLegacyData();
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
  // 打包模式：自有 Python 代码已编译加密成 core.bin，入口是 sourceless
  // _boot.pyc（安装目录里没有任何自有 .py 明文）。开发模式仍直接跑源码，
  // 便于调试，也不需要每次改代码都重新生成保护产物。
  const serverPath = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', '_boot.pyc')
    : path.join(__dirname, '..', 'backend', 'server.py');

  const env = Object.assign({}, process.env, {
    WCS_DATA_ROOT: DATA_ROOT,
    TEMP: DIRS.tmp,
    TMP: DIRS.tmp,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
  });

  // --version：把真实版本号交给 Python，检查更新要用它跟 GitHub 上的最新版比较。
  // 打包后 app.getVersion() 读的是 asar 里 package.json 的 version（构建时已被
  // build.js 改写成目标版本，例如 1.8.0），所以这里拿到的是对的版本。
  const child = spawn(
    cand.cmd,
    [...cand.args, serverPath, '0', '--token', TOKEN, '--quiet',
     '--version', app.getVersion()],
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
  // 设置页独立窗口：单例，已开就聚焦，避免开出多个
  let settingsWin = null;

  ipcMain.handle('wcs:open-settings', async () => {
    try {
      if (settingsWin && !settingsWin.isDestroyed()) {
        settingsWin.focus();
        return { ok: true, focused: true };
      }
      const uiPath = app.isPackaged
        ? path.join(process.resourcesPath, 'frontend', 'settings.html')
        : path.join(__dirname, '..', 'frontend', 'settings.html');

      settingsWin = new BrowserWindow({
        width: 980,
        height: 900,
        minWidth: 720,
        minHeight: 560,
        title: '设置 · 微信定时群发控制台',
        backgroundColor: '#f7f8fa',
        autoHideMenuBar: true,
        parent: win || undefined,     // 跟随主窗口，但用 show:true 保持独立
        webPreferences: {
          preload: path.join(__dirname, 'preload.js'),
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: false,
          // 与主窗口共享同一端口与 token —— 设置页要调同一套接口
          additionalArguments: [
            '--wcs-port=' + mainPort,
            '--wcs-token=' + TOKEN,
            '--wcs-dataroot=' + DATA_ROOT,
          ],
        },
      });
      settingsWin.loadFile(uiPath);
      settingsWin.on('closed', () => { settingsWin = null; });
      settingsWin.webContents.on('did-fail-load', (_e, code, desc, url) =>
        log('设置页加载失败 code=' + code + ' desc=' + desc + ' url=' + url)
      );
      settingsWin.webContents.on('console-message', (...args) => {
        if (args.length >= 4 && typeof args[1] === 'number') {
          log('settings[' + args[1] + '] ' + args[2]);
        } else if (args[0] && typeof args[0] === 'object') {
          log('settings[' + args[0].level + '] ' + args[0].message);
        }
      });
      return { ok: true };
    } catch (e) {
      log('打开设置页失败: ' + ((e && e.message) || e));
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  // 更新下载目录白名单。后端下载到 <数据根>/updates（绝不在 $INSTDIR 内），
  // 这里必须和 backend/update.py 的路径一致 —— 之前是 <安装目录>/temp/update
  // 对 <数据根>/data/temp/update，两边对不上，按钮本来就是坏的。
  function updateDir() { return path.resolve(DIRS.updates); }

  function checkedUpdateExe(p) {
    const allowedDir = updateDir();
    const real = path.resolve(String(p || ''));
    if (!real.startsWith(allowedDir + path.sep)) {
      return { error: '只允许打开更新下载目录里的文件' };
    }
    if (!/\.exe$/i.test(real)) return { error: '只支持 .exe 安装包' };
    if (!fs.existsSync(real)) return { error: '文件不存在' };
    return { path: real };
  }

  // 打开安装包所在文件夹（用户想自己双击时用，不执行任何文件）
  ipcMain.handle('wcs:open-path', async (_e, p) => {
    try {
      const c = checkedUpdateExe(p);
      if (c.error) return { ok: false, error: c.error };
      shell.showItemInFolder(c.path);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  // 安装更新：弹确认框 → 用户确认后才停 Python 边车、退出本程序，
  // 再由 detached 进程延迟拉起安装包。
  // 为什么必须退出：WeChatSender.exe / resources\python\python.exe 都在
  // $INSTDIR 内，NSIS 卸载段 `RMDir /r $INSTDIR` 删不掉被占用的 exe 会 Abort。
  // 为什么用延迟启动器：等主进程与边车真正退出后再运行安装包，
  // 否则安装包一起来就撞上还没释放的句柄。
  ipcMain.handle('wcs:install-update', async (_e, p) => {
    try {
      const c = checkedUpdateExe(p);
      if (c.error) return { ok: false, error: c.error };

      const r = await dialog.showMessageBox(win || null, {
        type: 'question',
        buttons: ['取消', '退出并安装'],
        defaultId: 1,
        cancelId: 0,
        title: '安装更新',
        message: '安装前需要关闭本程序',
        detail: '点击「退出并安装」后，本程序会立即退出并自动运行安装包。\n'
          + '如果有正在进行的发送任务，请先点「取消」并等它结束。\n\n'
          + '用户数据保存在安装目录之外，升级不会丢失。',
        noLink: true,
      });
      if (r.response !== 1) return { ok: false, cancelled: true };

      // PowerShell 延迟 3 秒再启动安装包。用单引号包路径并转义内部单引号，
      // 规避项目路径里的 & / 中文 / 空格。
      const exe = c.path.replace(/'/g, "''");
      const ps = 'Start-Sleep -Seconds 3; Start-Process -FilePath \'' + exe + '\'';
      const child = spawn('powershell.exe',
        ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
         '-Command', ps],
        { detached: true, stdio: 'ignore', windowsHide: true });
      child.unref();

      // 先停边车再退出；before-quit / window-all-closed 里还会各调一次（幂等）
      stopPython();
      setImmediate(() => app.quit());
      return { ok: true, quitting: true };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  // 数据根目录：界面显示 + 允许自定义（自定义写同级引导文件，重启生效）
  // 在系统文件管理器里显示用户数据目录（不执行任何文件）
  ipcMain.handle('wcs:reveal-data-root', async () => {
    try {
      ensureDir(DATA_ROOT);
      shell.showItemInFolder(DATA_ROOT);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  ipcMain.handle('wcs:data-root', async () => ({
    ok: true,
    root: DATA_ROOT,
    defaultRoot: DEFAULT_DATA_ROOT,
    pointer: DATA_ROOT_POINTER,
    installDir: APP_DIR,
    custom: path.resolve(DATA_ROOT) !== path.resolve(DEFAULT_DATA_ROOT),
    migrated: MIGRATED,
    isDev: IS_DEV,
  }));

  ipcMain.handle('wcs:set-data-root', async (_e, p) => {
    try {
      if (IS_DEV) return { ok: false, error: '开发模式不支持修改数据目录' };
      const raw = String(p || '').trim();
      // 空串 = 恢复默认：删掉引导文件即可。
      // 不能用 path.resolve('')（那会解析成当前工作目录）当路径写进去。
      if (!raw) {
        try { fs.unlinkSync(DATA_ROOT_POINTER); } catch (e) { /* 不存在就算了 */ }
        return { ok: true, root: DEFAULT_DATA_ROOT, needRestart: true,
                 note: '已恢复默认，重启后生效；原有数据不会自动移动' };
      }
      const want = path.resolve(raw);
      if (want === APP_DIR || want.startsWith(APP_DIR + path.sep)) {
        return { ok: false, error: '数据目录不能设在安装目录里（升级会被删除）' };
      }
      if (!isWritable(want)) return { ok: false, error: '该目录不可写' };
      ensureDir(APP_PARENT);
      fs.writeFileSync(DATA_ROOT_POINTER,
        JSON.stringify({ dataRoot: want }, null, 2), 'utf8');
      return { ok: true, root: want, needRestart: true,
               note: '下次启动生效；当前数据不会自动移动，请自行迁移或保持原路径' };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  ipcMain.handle('wcs:pick-directory', async () => {
    try {
      const r = await dialog.showOpenDialog(win || null, {
        title: '选择用户数据目录',
        properties: ['openDirectory', 'createDirectory'],
      });
      return r.canceled ? [] : r.filePaths;
    } catch (e) {
      return [];
    }
  });

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

// 记住主窗口用的服务端口：设置页要复用同一个后端，不能另起一个
let mainPort = 0;

function createWindow(port) {
  mainPort = port;
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
  const pid = pyChild.pid;
  // Windows 上 pyChild.kill() 只结束直接子进程；Python 边车若派生了子进程，
  // 它们仍会占用 resources\python\ 下的 dll/exe，导致升级安装包删不掉目录。
  // 这里补一次「进程树终止」，退出更干净。用 /T /F 且只在 Windows 上做。
  if (process.platform === 'win32' && pid) {
    try {
      spawn('taskkill', ['/PID', String(pid), '/T', '/F'],
            { stdio: 'ignore', windowsHide: true });
    } catch (e) { /* 忽略：下面还有 kill 兜底 */ }
  }
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
      '用户数据目录不可写',
      '用户数据保存在安装目录之外（升级/卸载不会被删除）：\n' + DATA_ROOT +
        '\n\n但该目录当前不可写。\n' +
        '请换一个可写的数据目录，或把程序装到可写位置，例如 D:\\WeChatSender。\n' +
        '（不要装在 C:\\Program Files 下，除非以管理员身份运行。）'
    );
    app.quit();
    return;
  }

  log('安装目录: ' + APP_DIR);
  log('用户数据根目录: ' + DATA_ROOT + (MIGRATED.length
      ? '（已从安装目录迁移: ' + MIGRATED.join(', ') + '）' : ''));

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