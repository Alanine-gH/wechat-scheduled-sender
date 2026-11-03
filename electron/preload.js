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
});
