"""配种亲缘计算与签发判定。

近交系数按 Malécot 共祖系数（coancestry）在谱系 DAG 上做记忆化递推，
自动合并重复祖先路径，不能只数共同祖先个数。设 P(x) 为个体 x 的亲代集合：

    phi(x, x) = (1 + F(x)) / 2                 （创始个体自身共祖为 1/2）
    phi(x, y) = (1/2) * sum(p in P(x)) phi(y, p)
                 （x != y 时展开世代更深的一方；无亲代的创始个体与无亲属
                  关系的他者共祖为 0）
    F(z) = phi(sire(z), dam(z))

拟产犊的近交系数即双亲共祖系数。所有分数用 fractions.Fraction 精确表示，
避免浮点比较在 13/100 门槛附近出错。
"""

from fractions import Fraction

from .studbook import ancestor_ids, Studbook

# 种畜站本题上限：拟产犊近交系数 <= 13/100 才可签发
DEFAULT_THRESHOLD = Fraction(13, 100)
DECISION_SURFACE = '配种签发'


def shared_parent_count(animals, first, second):
    """一代共享父母计数（基线兼容：A/B 共用父本 S 时返回 1）。"""
    return len(set(ancestor_ids(animals, first))
               & set(ancestor_ids(animals, second)))


class KinshipCalculator:
    """在一份不可变视角的谱系册上做共祖/近交递推。"""

    def __init__(self, book):
        if not isinstance(book, Studbook):
            raise TypeError('KinshipCalculator 需要 Studbook 实例')
        book.validate()
        self.book = book
        self._phi = {}
        self._rank = {}
        self._book_revision = -1
        self._build_ranks()

    def _build_ranks(self):
        """按当前拓扑序重建世代排名；谱系修订后惰性失效。"""
        self._rank = {}
        for node in self.book.topological_order():
            parents = [p for p in self.book.parents(node) if p]
            self._rank[node] = (
                1 + max(self._rank[p] for p in parents)) if parents else 0
        self._book_revision = self.book.revision
        self._phi = {}

    def rank(self, name):
        """拓扑世代排名：创始个体为 0，沿最长亲代链递增。"""
        canonical = self.book.resolve(name)
        if self.book.revision != self._book_revision:
            self._build_ranks()
        return self._rank[canonical]

    def phi(self, first, second, _visiting=None):
        """两个体的共祖系数 phi(first, second)，精确分数。"""
        if self.book.revision != self._book_revision:
            self._build_ranks()
        x = self.book.resolve(first)
        y = self.book.resolve(second)
        key = (x, y) if x <= y else (y, x)
        if key in self._phi:
            return self._phi[key]
        if _visiting is None:
            _visiting = frozenset()
        if key in _visiting:
            raise ValueError('共祖递推出现回访，谱系疑似含环')
        # 同一个体：phi(x,x) = (1 + F(x)) / 2，创始个体为 1/2
        if x == y:
            value = (Fraction(1) + self.inbreeding(x)) / 2
            self._phi[key] = value
            return value
        # 始终展开世代更深的一方，保证递归朝 rank 更小的方向终止
        if self.rank(y) > self.rank(x):
            x, y = y, x
        parents = [p for p in self.book.parents(x) if p]
        if not parents:
            value = Fraction(0)  # 互不为亲属的创始个体
        else:
            total = Fraction(0)
            for parent in parents:
                total += self.phi(y, parent, _visiting | {key})
            value = total / 2
        self._phi[key] = value
        return value

    def inbreeding(self, name):
        """个体自身的近交系数 F = phi(父, 母)，创始个体为 0。"""
        canonical = self.book.resolve(name)
        sire, dam = self.book.parents(canonical)
        if not sire or not dam:
            return Fraction(0)
        return self.phi(sire, dam)

    def projected_inbreeding(self, first, second):
        """拟产犊近交系数 = 双亲共祖系数。"""
        return self.phi(first, second)

    def _upward_paths(self, name):
        """从个体出发的全部简单上行路径（每条含起点，止于某祖先）。"""
        canonical = self.book.resolve(name)
        paths = [[canonical]]
        for parent in self.book.parents(canonical):
            if parent is None:
                continue
            for sub_path in self._upward_paths(parent):
                paths.append([canonical] + sub_path)
        return paths

    def wright_decomposition(self, first, second):
        """Wright 路径法逐项展开，作为递推结果的可审计证据。

        每条连接双亲、经共同祖先 A 的路径贡献 (1+F(A))/2^n；若路径内部
        出现重复个体（两条上行链在非共同祖先处相交），该路径项无效，
        必须剔除——这正是“只数共同祖先数量”会高估的原因。
        """
        first = self.book.resolve(first)
        second = self.book.resolve(second)
        terms = []
        for path_first in self._upward_paths(first):
            for path_second in self._upward_paths(second):
                ancestor = path_first[-1]
                if ancestor != path_second[-1]:
                    continue
                inner = path_first[:-1] + path_second[:-1]
                valid = (len(inner) == len(set(inner))
                         and ancestor not in inner)
                edges = len(path_first) + len(path_second) - 1
                term = (1 + self.inbreeding(ancestor)) / (2 ** edges)
                terms.append({
                    'ancestor': ancestor,
                    'path_first': list(path_first),
                    'path_second': list(path_second),
                    'edges': edges,
                    'ancestor_inbreeding': _fraction_tuple(
                        self.inbreeding(ancestor)),
                    'contribution': _fraction_tuple(term),
                    'valid': valid,
                })
        valid_sum = sum((Fraction(*term['contribution'])
                         for term in terms if term['valid']), Fraction(0))
        recursive = self.phi(first, second)
        return {
            'terms': terms,
            'valid_sum': _fraction_tuple(valid_sum),
            'recursive_phi': _fraction_tuple(recursive),
            'matches': valid_sum == recursive,
        }


def kinship(book, first, second):
    return KinshipCalculator(book).phi(first, second)


def inbreeding_coefficient(book, name):
    return KinshipCalculator(book).inbreeding(name)


def projected_inbreeding(book, first, second):
    return KinshipCalculator(book).projected_inbreeding(first, second)


def evaluate_pair(book, first, second, threshold=DEFAULT_THRESHOLD):
    """对一对候选种畜出具拟产犊近交评估与签发/拒签结论。"""
    threshold = Fraction(threshold)
    calculator = KinshipCalculator(book)
    coefficient = calculator.projected_inbreeding(first, second)
    canonical_first = book.resolve(first)
    canonical_second = book.resolve(second)
    # 世代按 1 起编号：创始个体为第 1 代，拟产犊为双亲世代 +1
    projected_generation = 2 + max(
        book.depth(canonical_first), book.depth(canonical_second))
    return {
        'surface': DECISION_SURFACE,
        'pair': [canonical_first, canonical_second],
        'coefficient': coefficient,
        'numerator': coefficient.numerator,
        'denominator': coefficient.denominator,
        'decimal': f'{float(coefficient):.6f}',
        'threshold': threshold,
        'threshold_decimal': f'{float(threshold):.6f}',
        'projected_generation': projected_generation,
        'decision': '签发' if coefficient <= threshold else '拒签',
        'decomposition': calculator.wright_decomposition(
            canonical_first, canonical_second),
    }


def issue(book, first, second, threshold=DEFAULT_THRESHOLD, evidence_ids=()):
    """配种签发：评估结论附证据 ID 列表。"""
    evaluation = evaluate_pair(book, first, second, threshold)
    evaluation['evidence_ids'] = sorted(set(evidence_ids))
    return evaluation


def _fraction_tuple(value):
    return [value.numerator, value.denominator]


def serialize_result(obj):
    """递归把评估结果中的 Fraction 转为 [分子, 分母]，供 JSON 落库。"""
    if isinstance(obj, Fraction):
        return _fraction_tuple(obj)
    if isinstance(obj, dict):
        return {key: serialize_result(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_result(value) for value in obj]
    return obj


def build_studbook(events):
    """消费证据事件流，返回 (Studbook, 导入统计)。"""
    return Studbook.from_events(events)


def apply_parentage_review(book, event):
    """应用签名基因亲权复核，发布亲权更正。

    旧边保留为 superseded，旧签发计算不被覆写；返回更正后的边信息。
    """
    if event.get('kind') != 'parentage_review':
        raise ValueError('事件不是 parentage_review 亲权复核')
    data = event.get('data', {})
    changed = book.replace_parent(
        data['child'], data['role'], data['new_parent'],
        event['id'], rationale=data.get('rationale'))
    return {'changed': changed,
            'child': book.resolve(data['child']),
            'role': data['role'],
            'new_parent': book.resolve(data['new_parent']),
            'old_parent': data.get('old_parent'),
            'evidence_id': event['id']}


def pedigree_events(events, scenario=None):
    """按 (event_time, id) 排序并可按 scenario 过滤证据事件。

    scenario='baseline' 只保留未标记为更正场景的原始证据；
    scenario='parentage_correction' 保留全部事件（建册后再应用复核）。
    """
    ordered = sorted(events, key=lambda event: (event['event_time'], event['id']))
    if scenario is None or scenario == 'parentage_correction':
        return ordered
    return [event for event in ordered
            if event.get('data', {}).get('scenario') in (None, scenario)]
