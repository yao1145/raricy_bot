# Cattca 脚本语法

## Cattca 脚本的基本结构和注释是什么？

脚本由 Markdown 文本和 `</命令1; 命令2;/>` 命令块交替组成。命令块可跨行，分号是命令分隔符；块外文字正常显示。注释 `/* ... */` 可放在文本或命令块中，不显示、不执行，不能嵌套。

## Cattca let 怎样声明变量？变量名怎么写？

语法 `let 变量名` 或 `let 变量名 = 表达式`，初值可省略。名字以字母或下划线开头，由字母、数字和下划线组成，区分大小写。

```text
</let name = "莱莉菥";
let hp = 100;
let maxHp = 100;
let isAlive = true;/>
```

## Cattca set 怎样修改已有变量？

语法 `set 变量名 = 表达式`，变量需先用 `let` 声明；修改未声明变量会报运行时错误。

```text
</set hp = hp - 10;
set name = "受伤的莱莉菥";/>
```

## Cattca apply 怎样行内输出表达式？

语法 `apply 表达式`，输出与前后的正文拼接，不额外换段。

```text
生命值 </apply hp /> 点。
剩余百分比：</apply hp / maxHp * 100 />%。
```

## Cattca label 和 goto 怎样跳转？标签不存在会怎样？

`label 标签名` 定义位置，不产生输出；`goto 标签名` 跳到这个位置，标签名区分大小写。例如 `</label start;/>` 和 `</goto start;/>`。跳转到不存在的标签会在日志中警告，脚本继续执行。

## Cattca if 怎样执行条件命令？

语法 `if 表达式 -> 命令`，条件为真时执行右侧单条命令，可以是 `goto`、`set` 或 `exit` 等。

```text
</if hp <= 0 -> goto gameOver;
if hasKey -> goto openDoor;/>
```

## Cattca exit 怎样结束故事？

`</exit/>` 立即结束脚本。把它放在分支结局末尾，可阻止后面其他结局继续显示。

## Cattca input case 的完整语法是什么？

语法 `input case [变量名]: '选项文字' -> 命令: '另一个选项' -> 命令`。变量名可省略；提供时保存选中的选项文字。选项间用英文冒号，最后一个后不加冒号。

```text
</input case choice:
'打开宝箱' -> goto treasure:
'继续前进' -> goto nextRoom:
'原路返回' -> exit;/>
```

## Cattca input text 怎样读取文本？

`input text 变量名` 等待输入并存入变量，变量必须先用 `let` 声明。提示文字写在命令块外：

```text
</let playerName = "";/>
请输入你的名字：
</input text playerName;/>
欢迎，</apply playerName />！
```

## Cattca log 怎样输出调试信息？

`log 表达式` 把内容写到运行区底部的调试日志，不输出到故事正文。例如 `</log "进入房间，hp=" + hp;/>`。

## Cattca random 的范围包含两端吗？能用表达式吗？

`random 变量名 = 最小值 最大值` 生成包含两端的随机整数，变量必须先声明。范围可以是表达式，例如 `random atk = baseAtk baseAtk+10`。

```text
</let dice = 0;
random dice = 1 6;/>
骰子点数：</apply dice />
```

## Cattca 表达式支持什么字面量和转义？

支持数字（`42`、`-3`、`3.14`）、字符串（`"hello"`、`'你好'`）、布尔（`true`、`false`）、空值 `null`。字符串可用 `\n` 换行、`\r` 回车、`\t` 制表符。读取变量直接写变量名。

```text
</let a = 10;
let b = a * 2;
apply a + b;/>
```

这个示例输出 30。

## Cattca 支持哪些算术、比较和逻辑运算？

| 类别 | 运算符 |
|---|---|
| 算术 | `+` `-` `*` `/` `%` |
| 比较 | `==` `!=` `>` `<` `>=` `<=` |
| 逻辑 | `&&` `\|\|` `!` |
| 三元 | `条件 ? 真值 : 假值` |

表达式可用于 `let`、`set`、`if`、`apply`、`log` 和 `random` 的值位置。

## Cattca 全部命令有哪些？

| 命令 | 语法 | 用途 |
|---|---|---|
| let | `let name = expr` | 声明变量，赋值可省略 |
| set | `set name = expr` | 修改已声明变量 |
| apply | `apply expr` | 行内输出 |
| label | `label name` | 定义标签 |
| goto | `goto name` | 跳转标签 |
| if | `if cond -> cmd` | 条件执行 |
| exit | `exit` | 结束脚本 |
| input case | `input case [var]: 'opt' -> cmd : ...` | 按钮选择 |
| input text | `input text var` | 自由输入 |
| log | `log expr` | 调试日志 |
| random | `random var = min max` | 随机整数 |

## Cattca 怎样写会循环的完整故事？

下面用计数器记录继续睡觉次数，超过 3 次进入睡觉结局；选择起床进入起床结局。

```text
# 莱莉菥的早晨
你醒了，早上六点。
</let cntsleep = 0;
label start;
input case:
'继续睡觉' -> goto sleep:
'起床' -> goto wakeup;/>

</label sleep;
set cntsleep = cntsleep + 1;
if cntsleep > 3 -> goto ending;/>
你睡不着。已尝试 </apply cntsleep /> 次。
</goto start;/>

</label wakeup;/>
你成功起床了！
</exit/>

</label ending;/>
你又睡着了。
</exit/>
```
