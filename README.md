# 草场轮牧载畜采证台

草场许可、牲畜群转场、植被样方和休牧恢复证据共同决定草场能否继续放牧及超载补偿；巡护设备可离线补传。

三套子系统通过原始证据、SQLite 状态与版本化决定互相影响。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m rangeledger init --db work.db
python -m rangeledger demo --db work.db
python -m unittest discover -s tests -v
```

命令：permit, movement, recovery。每个命令通过 `--fixture` 读取单一子系统的 JSON；`demo` 依次执行正常样例。每项报告列证据 ID 和决定版本。

