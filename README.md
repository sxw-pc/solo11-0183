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


## 种畜站谱系与配种近交系数

`fixtures/breeding-pedigree.json` 保存五代谱系证据。G/H/J/K/L 为互不相关创始个体，P=(G,H)、Q=(G,J)、X=(P,Q)、A=(X,K)、B=(X,L)。A、B 是同父异母，只查一代共享父母会得到 1，但 X 本身由同父异母的 P、Q 产生，F(X)=1/8，A×B 拟产犊的真实近交系数为 (1+1/8)/8=9/64。

近交系数在 `Studbook` 的祖谱 DAG 上用 Malécot 共祖系数记忆化递推（`rangeledger/breeding.py`），自动合并重复祖先路径；Wright 路径分解逐项列出有效/无效路径作为审计证据，链内出现重复个体的路径项剔除，不能按共同祖先数量直接计数。种畜站门槛为 13/100：9/64 拒签、1/8 签发，分数用 `Fraction` 精确比较。

两条获授权路线：

- 原始谱系证据有效：拒签 A×B，改签与创始父母所生、无关联的 C，A×C 系数为 0。
- 收到签名基因亲权复核（`parentage_review`），证明 Q 的父本是新创始个体 M 而非 G：发布祖谱更正版，旧 Q-G 边保留为 `superseded` 并挂接复核证据，F(X) 降为 0，A×B 变为 1/8 可签；旧签发计算与旧快照仍可追溯，不做覆写。

录入校验每个 ID、按时间排序、检测祖谱环与自交、别名冲突（同一耳标不能归并给两个个体，亲缘计算只在规范 ID 上进行）、子女出生晚于父母；重复导入幂等，冲突登记报错。`pedigree_snapshot` 与 `breeding_decision` 表按版本保存祖谱快照与配种签发/拒签决定。
