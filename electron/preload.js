'use strict';
/**
 * 预加载脚本：把主进程传来的服务端口与 token 安全地交给界面。
 *
 * contextIsolation 开着，渲染进程拿不到 Node，只能通过这里显式暴露的只读对象
 * 访问本地服务。
 */
const { contextBridge } = require('electron');

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
});