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


## 配额、占用与恢复语义

许可期限与畜群实际入离场时间比较。转让事件按时间依次更新出让方和受让方余额；余额不足的转让判为超配。占用按草场、耳标畜群和牧户分组，重叠的同一畜群时段只取一次最大头数；防疫隔离阻止合法通行，但实际占用仍计入生态压力。夜间越界以每天 20:00–次日 06:00 的区间交集计算小时，补偿按牧户压力占比和越界时长分摊。

休牧令保持历史。撤销活跃休牧令后，必须收到晚于原命令的植被样方，且占用、草量与检疫条件都允许，才能重开。`python -m rangeledger replay --db work.db --subject case-1` 从原始证据重算十项决定，发现被篡改的最新报告时返回差异。
