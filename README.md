# 草场轮牧载畜采证台

草场许可、牲畜群转场、植被样方和休牧恢复证据共同决定草场能否继续放牧及超载补偿；巡护设备可离线补传。

三套子系统通过原始证据、SQLite 状态与版本化决定互相影响。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m rangeledger init --db work.db
python -m rangeledger demo --db work.db
python -m unittest discover -s tests -v
```

命令：permit, movement, recovery, schedule。每个命令通过 `--fixture` 读取单一子系统的 JSON；`demo` 依次执行正常样例。每项报告列证据 ID 和决定版本。

## 跨牧户预约的全局分配（schedule）

旱季多牧户同窗预约时，单条准入回执不足以放行；`schedule` 把离线预约清单（`booking`）、每区草量与容量快照（`vegetation_sample`/`zone_capacity`）、群级禁行边界（`boundary`）和许可签认（`permit`）汇成可审计的当班全局分配表：

- 受理台账按 `received_at` 回放，重复预约、同群跨区冲突、先到取消、时区折返与已入场后改期均留痕（异常写入 `rejection` 表，历史版本不删除）。
- 局部贪心按申请先后把每群派到第一处同窗有余量的草区；全局回溯在同一组完整输入上搜索让全部预约落地的交换方案。合成案例中贪心只放甲群、交换方案把甲群换到 Z2、乙群留 Z1，两群各占 4 动物小时。
- 签认协议决定可交换性：仅签时间未锁区时可交换；牧户与防疫员双签锁区当天禁止自动转签，被阻挡牧户进入“待改期”，不伪造他区许可，禁行依据写入被阻挡约束台账。
- 放行结果投影为带 `alloc_version`、来源预约与来源许可的 `movement` 事件，再沿 permit→movement→recovery 复核许可期限、分区压力与恢复开放状态；草量补录晚到只生成新决定版本，旧的未分配决定不会被改写成已入场事实。


## 配额、占用与恢复语义

许可期限与畜群实际入离场时间比较。转让事件按时间依次更新出让方和受让方余额；余额不足的转让判为超配。占用按草场、耳标畜群和牧户分组，重叠的同一畜群时段只取一次最大头数；防疫隔离阻止合法通行，但实际占用仍计入生态压力。夜间越界以每天 20:00–次日 06:00 的区间交集计算小时，补偿按牧户压力占比和越界时长分摊。

休牧令保持历史。撤销活跃休牧令后，必须收到晚于原命令的植被样方，且占用、草量与检疫条件都允许，才能重开。`python -m rangeledger replay --db work.db --subject case-1` 从原始证据重算十项决定，发现被篡改的最新报告时返回差异。
