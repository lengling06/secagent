# obfuscator.io / webpack 混淆

**何时用此 skill**: 美化后 JS 看到这些特征:
- 大数组 `var _0xabcd = ['xxx','yyy',...]` + 数组旋转函数
- 字符串调用 `_0xabcd[0x1]('...')` 这种十六进制下标
- 控制流扁平化: `while(true){switch(_0x){case 0:...; case 1:...;}}`
- 死代码注入 / 字符串 base64 / RC4 包裹
- 函数名全是 `_0x` 开头的随机十六进制

## 工具优先级

1. **webcrack** — 现代首选, 一条命令撤大部分 obfuscator.io 套路
   ```bash
   npx webcrack input.js -o output/
   ```
   能自动还原: 字符串数组、控制流扁平化、死代码消除、属性重命名。

2. **synchrony** — 老牌但仍维护, 处理 javascript-obfuscator 输出
   ```bash
   npx synchrony deobfuscate input.js
   ```

3. **手动 + ast_grep** — webcrack 失败时回退, 针对性写 codemod

## 工作流

1. `js_beautify` 先美化压缩
2. 看头 200 行判断混淆类型 (是否 `_0xabcd` 大数组、是否 case-switch dispatcher)
3. 命中 obfuscator.io 特征 → `shell` 跑 `npx webcrack`
4. 输出落到 `engagement/js/<host>/deobf/`
5. 在 deobf 版本里再搜签名/加密关键字

## 不要做

- ❌ 在还混淆的代码里直接搜 "sign" / "encrypt" — 字符串都被数组化了, 搜不到
- ❌ 让 agent 在脑里"反推"控制流 — 浪费 token, 用工具
- ❌ 在 webcrack 输出后再用 obfuscator.io 检测器跑一遍 — 已经不是同种混淆了

## 反例

- 撞过的坑: 用 webcrack 解了之后忘记重新 grep, 还在原始混淆版里找 → 当然找不到
- webcrack 对 v8 obfuscator 效果差; 那个用 synchrony

## 提示词片段

> 小霜大人, 这个 bundle 是 obfuscator.io 标准套路 (大数组+控制流扁平化); 先 `npx webcrack` 反混淆, 输出到 `js/<host>/deobf/`, 再在 deobf 版里搜签名。
