/**
 * client.js 离线自检：
 * 用假的 window.__ModuleLoader__ 和假的 react 执行插件脚本，
 * 确认它能正常注册、不抛错、不引用 module.exports。
 *
 * 不需要浏览器，也不依赖真实宿主。
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const CLIENT = path.join(HERE, '..', 'client.js')

let captured = null
const registrations = []

// 假 React：只要能被调用、能取 createElement 即可
const fakeReact = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  useState: (init) => [typeof init === 'function' ? init() : init, () => {}],
  useCallback: (fn) => fn,
  useRef: (init) => ({ current: init }),
  useEffect: () => {},
  Fragment: 'Fragment'
}

const fakeCtx = {
  slots: {
    inject(ownerKey, fn) {
      registrations.push(['inject', ownerKey])
      // 真实宿主会在 owner 声明就绪时调用这个回调，自检里直接执行
      try {
        fn()
      } catch (error) {
        console.error(`[FAIL] inject(${ownerKey}) 的回调抛错：`, error)
        process.exit(1)
      }
    },
    register(options, Component) {
      registrations.push(['register', options?.name, options?.id ?? options?.key])
      if (typeof Component !== 'function') {
        console.error(`[FAIL] ${options?.name} 注册的组件不是函数`)
        process.exit(1)
      }
      // 尝试渲染一次，确认组件本身不炸
      try {
        const out = Component({})
        if (out === undefined) {
          console.error(`[FAIL] ${options?.name} 组件返回 undefined`)
          process.exit(1)
        }
      } catch (error) {
        console.error(`[FAIL] ${options?.name} 组件渲染抛错：`, error)
        process.exit(1)
      }
      return () => {}
    }
  }
}

globalThis.window = {
  __ModuleLoader__: {
    load(def) {
      captured = def
    }
  }
}

const source = readFileSync(CLIENT, 'utf8')
// 经典脚本直接在全局执行
// eslint-disable-next-line no-new-func
const run = new Function('window', 'module', source)

try {
  run(globalThis.window, undefined)
} catch (error) {
  console.error('[FAIL] 脚本执行抛错：', error)
  process.exit(1)
}

if (!captured) {
  console.error('[FAIL] 没有调用 window.__ModuleLoader__.load')
  process.exit(1)
}

if (captured.id !== 'dsh-cad-draw') {
  console.error('[FAIL] 模块 id 不对：', captured.id)
  process.exit(1)
}

let plugin
try {
  plugin = captured.factory((name) => {
    if (name === 'react') return fakeReact
    throw new Error('未预期的依赖：' + name)
  })
} catch (error) {
  console.error('[FAIL] factory 抛错：', error)
  process.exit(1)
}

if (!plugin || typeof plugin.apply !== 'function') {
  console.error('[FAIL] factory 没有返回带 apply 的插件对象')
  process.exit(1)
}

try {
  plugin.apply(fakeCtx)
} catch (error) {
  console.error('[FAIL] apply 抛错：', error)
  process.exit(1)
}

const registered = registrations.filter((r) => r[0] === 'register')
if (registered.length === 0) {
  console.error('[FAIL] 没有注册任何 slot')
  process.exit(1)
}

console.log('[OK] client.js 自检通过')
console.log('     模块 id :', captured.id)
console.log('     inject  :', plugin.inject)
for (const [, slot, key] of registered) {
  console.log(`     注册    : ${slot}  (${key})`)
}
