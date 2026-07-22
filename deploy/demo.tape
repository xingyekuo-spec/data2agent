# README 首屏参考 GIF 的录制脚本(charmbracelet VHS,声明式、可复现)
#
#   安装:brew install vhs        (拉 ffmpeg / ttyd 依赖)
#   渲染:vhs deploy/demo.tape    (在仓库根目录运行,产出 docs/assets/demo.gif)
#
# 内容:全链路 40 秒 —— 参考库 seed → 只读增量抽取 → 物化对象层 →
#       Agent 视角的接单评审建议卡(数字可溯源)。

Output docs/assets/demo.gif

Set FontSize 15
Set FontFamily "Sarasa Mono SC"
Set Width 1160
Set Height 700
Set Padding 14
Set TypingSpeed 30ms
Set Theme "Catppuccin Mocha"

Type "# data2agent —— 把工厂数据接给 AI Agent(参考链)"
Enter
Sleep 1.5s

Type ".venv/bin/python -m data2agent.showroom.seed"
Enter
Sleep 2.5s

Type ".venv/bin/python -m data2agent.connect sync --sqlite showroom/e10.sqlite"
Enter
Sleep 3.5s

Type ".venv/bin/python -m data2agent.connect apply"
Enter
Sleep 2.5s

Type "clear"
Enter
Sleep 500ms

Type ".venv/bin/python -m data2agent.showroom.review_demo"
Enter
Sleep 9s
