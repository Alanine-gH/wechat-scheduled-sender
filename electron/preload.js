'use strict';
/**
 * 预加载脚本：把主进程传来的服务端口与 token 安全地交给界面。
 *
 * contextIsolation 开着，渲染进程拿不到 Node，只能通过这里显式暴露的只读对象
 * 访问本地服务。
 *
 * pickFiles：浏览器里 <input type=file> 出于安全只给文件名、拿不到绝对路径，
 * 而发送素材需要真实路径，所以走 IPC 让主进程弹系统文件选择框。
 */
const { contextBridge, ipcRenderer } = require('electron');

function argValue(prefix) {
  const hit = process.argv.find((a) => a.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : '';
}

const port = parseInt(argValue('--wcs-port='), 10) || 0;
const token = argValue('--wcs-token=');
const dataRoot = argValue('--wcs-dataroot=');

contextBridge.exposeInMainWorld('__WCS__', {
  mode: 'electron',
  port,
  token,
  dataRoot,
  baseUrl: port ? `http://127.0.0.1:${port}` : '',
  // 返回 Promise<string[]>（用户取消则返回空数组）
  pickFiles: () => ipcRenderer.invoke('wcs:pick-files'),
  // 开机自启：get -> {ok,on,packaged}；set -> {ok,on} 或 {ok:false,error}
  getAutostart: () => ipcRenderer.invoke('wcs:get-autostart'),
  setAutostart: (on) => ipcRenderer.invoke('wcs:set-autostart', !!on),
  // 打开独立设置窗口（单例，已开则聚焦）
  openSettings: () => ipcRenderer.invoke('wcs:open-settings'),
  // 打开更新下载目录里的安装包所在文件夹（主进程校验路径白名单）
  openPath: (p) => ipcRenderer.invoke('wcs:open-path', String(p || '')),
  // 安装更新：主进程弹确认框，确认后停边车、退出本程序、延迟启动安装包
  installUpdate: (p) => ipcRenderer.invoke('wcs:install-update', String(p || '')),
  // 用户数据根目录：查询 / 自定义（自定义写同级引导文件，重启生效）
  getDataRoot: () => ipcRenderer.invoke('wcs:data-root'),
  setDataRoot: (p) => ipcRenderer.invoke('wcs:set-data-root', String(p || '')),
  // 选择文件夹（用于自定义数据目录）
  pickDirectory: () => ipcRenderer.invoke('wcs:pick-directory'),
  revealDataRoot: () => ipcRenderer.invoke('wcs:reveal-data-root'),
  // 关闭当前窗口（设置页的「关闭」按钮用；浏览器模式走 window.close）
  closeSettings: () => {
    try { window.close(); } catch (_) {}
  },
});
