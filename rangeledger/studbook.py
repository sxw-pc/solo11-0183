"""种畜站谱系册：带每条亲子来源证据的祖谱有向无环图（DAG）。

每个个体只有一个规范 ID，耳标/旧名等一律作为别名归并到规范 ID，亲缘
计算只在规范 ID 上进行，避免同一个体被别名重复计数。每条父子/母子边都
记录来源证据 ID；更正亲权时旧边保留为 superseded，不做物理覆写。

录入时校验：每个 ID 必须已登记、父母不能是同一个体、亲子不能成环、子女
出生时间必须晚于父母、别名不能同时指向两个个体。
"""


class PedigreeError(ValueError):
    """谱系领域错误基类。"""


class UnknownIdentityError(PedigreeError):
    """引用了未登记的个体 ID。"""


class AliasConflictError(PedigreeError):
    """同一别名指向多个个体，或个体被别名重复计数。"""


class PedigreeCycleError(PedigreeError):
    """亲子关系构成环，祖谱必须是 DAG。"""


class PedigreeValidationError(PedigreeError):
    """自交、出生时序等结构性校验失败。"""


def ancestor_ids(animals, animal_id):
    """返回一代父母 ID（兼容基线字典式谱系）。"""
    animal = animals.get(animal_id, {})
    return [value for value in (animal.get('sire'), animal.get('dam')) if value]


class Studbook:
    """内存中的谱系 DAG，可序列化为快照并从快照重建。"""

    def __init__(self):
        self._animals = {}          # 规范 ID -> 个体记录
        self._alias = {}            # 别名 -> 规范 ID
        self._edges = {}            # (子女, 亲代) -> 边记录（含 superseded）
        self._evidence = set()      # 已采用的证据 ID
        self._depth_cache = {}
        self.revision = 0           # 任何结构/亲代变动都会递增

    # ---------------------------------------------------------------- 录入
    def add_animal(self, animal_id, sire=None, dam=None, aliases=(),
                   born=None, evidence_id=None):
        """登记一个个体。完全相同的重复登记幂等跳过，返回 False；否则 True。"""
        if not isinstance(animal_id, str) or not animal_id:
            raise PedigreeValidationError('个体 ID 不能为空字符串')
        if animal_id in self._animals:
            return self._merge_animal(animal_id, sire, dam, aliases, born, evidence_id)
        if self._alias.get(animal_id) is not None:
            raise AliasConflictError(
                f'ID {animal_id!r} 已作为 {self._alias[animal_id]!r} 的别名占用')

        canon_aliases = []
        for alias in aliases:
            self._bind_alias(alias, animal_id)
            canon_aliases.append(alias)

        sire_c = self._resolve_parent(sire, animal_id)
        dam_c = self._resolve_parent(dam, animal_id)
        if sire_c and sire_c == dam_c:
            raise PedigreeValidationError(f'{animal_id} 的父本与母本不能是同一个体')
        if animal_id in (sire_c, dam_c):
            raise PedigreeCycleError(f'{animal_id} 不能成为自己的亲代')
        for parent in (sire_c, dam_c):
            if parent and animal_id in self.ancestors(parent):
                raise PedigreeCycleError(
                    f'登记 {animal_id} 的亲代 {parent} 会形成祖谱环')
        if born is not None:
            for parent in (sire_c, dam_c):
                parent_born = self._animals[parent]['born'] if parent else None
                if parent_born is not None and parent_born >= born:
                    raise PedigreeValidationError(
                        f'{animal_id} 的出生时间必须晚于亲代 {parent}')

        record = {
            'id': animal_id,
            'sire': sire_c,
            'dam': dam_c,
            'aliases': tuple(canon_aliases),
            'born': born,
            'evidence_ids': tuple([evidence_id]) if evidence_id else (),
        }
        self._animals[animal_id] = record
        if evidence_id:
            self._evidence.add(evidence_id)
        for role, parent in (('sire', sire_c), ('dam', dam_c)):
            if parent:
                self._edges[(animal_id, parent)] = {
                    'child': animal_id, 'parent': parent, 'role': role,
                    'status': 'active',
                    'evidence_ids': [evidence_id] if evidence_id else [],
                }
        self._depth_cache.clear()
        self.revision += 1
        return True

    def _merge_animal(self, animal_id, sire, dam, aliases, born, evidence_id):
        record = self._animals[animal_id]
        sire_c = self._resolve_parent(sire, animal_id) if sire else None
        dam_c = self._resolve_parent(dam, animal_id) if dam else None
        # 环检测优先：给既有个体补登亲代也不能成环或自交
        for parent in (sire_c, dam_c):
            if parent == animal_id:
                raise PedigreeCycleError(f'{animal_id} 不能成为自己的亲代')
            if parent and animal_id in self.ancestors(parent):
                raise PedigreeCycleError(
                    f'给 {animal_id} 补登亲代 {parent} 会形成祖谱环')
        if sire_c and dam_c and sire_c == dam_c:
            raise PedigreeValidationError(
                f'{animal_id} 的父本与母本不能是同一个体')
        if (sire_c is not None and sire_c != record['sire']) or \
           (dam_c is not None and dam_c != record['dam']):
            raise PedigreeValidationError(
                f'{animal_id} 重复登记的亲代与既有证据冲突')
        if born is not None and record['born'] is not None and born != record['born']:
            raise PedigreeValidationError(
                f'{animal_id} 重复登记的出生时间与既有证据冲突')
        changed = False
        for alias in aliases or ():
            if alias not in record['aliases']:
                self._bind_alias(alias, animal_id)
                record['aliases'] = record['aliases'] + (alias,)
                changed = True
        if evidence_id and evidence_id not in self._evidence:
            self._evidence.add(evidence_id)
            record['evidence_ids'] = record['evidence_ids'] + (evidence_id,)
            for parent in (record['sire'], record['dam']):
                if parent:
                    edge = self._edges[(animal_id, parent)]
                    if evidence_id not in edge['evidence_ids']:
                        edge['evidence_ids'].append(evidence_id)
            changed = True
        return changed

    def _bind_alias(self, alias, canonical):
        if not isinstance(alias, str) or not alias:
            raise PedigreeValidationError('别名不能为空字符串')
        if alias == canonical:
            return
        if alias in self._animals:
            raise AliasConflictError(
                f'别名 {alias!r} 本身已是登记个体，不能再归并给 {canonical!r}')
        owner = self._alias.get(alias)
        if owner is not None and owner != canonical:
            raise AliasConflictError(
                f'别名 {alias!r} 同时指向 {owner!r} 与 {canonical!r}')
        self._alias[alias] = canonical

    def _resolve_parent(self, name, child):
        if name is None:
            return None
        try:
            return self.resolve(name)
        except UnknownIdentityError as exc:
            raise UnknownIdentityError(
                f'个体 {child} 引用了未登记的亲代 {name!r}') from exc

    # ---------------------------------------------------------------- 查询
    def resolve(self, name):
        """把任意规范 ID/别名解析为规范 ID。"""
        if name in self._animals:
            return name
        canonical = self._alias.get(name)
        if canonical is not None:
            return canonical
        raise UnknownIdentityError(f'未登记的个体 {name!r}')

    def __contains__(self, name):
        try:
            self.resolve(name)
            return True
        except UnknownIdentityError:
            return False

    def __len__(self):
        return len(self._animals)

    def ids(self):
        return sorted(self._animals)

    def animal(self, name):
        return dict(self._animals[self.resolve(name)])

    def parents(self, name):
        """活跃亲代（父, 母），缺失位为 None。"""
        record = self._animals[self.resolve(name)]
        return record['sire'], record['dam']

    def edge(self, child, parent):
        return dict(self._edges[(self.resolve(child), self.resolve(parent))])

    def edges(self, include_superseded=True):
        keys = sorted(self._edges, key=lambda key: (key[0], key[1]))
        return [dict(self._edges[key]) for key in keys
                if include_superseded or self._edges[key]['status'] == 'active']

    def ancestors(self, name):
        """全部上代祖先（不含自身），沿活跃边遍历，自动去重。"""
        start = self.resolve(name)
        seen = set()
        queue = [p for p in self.parents(start) if p]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(p for p in self.parents(current) if p)
        return seen

    def depth(self, name):
        """世代深度：创始个体为 0，取到创始个体的最长路径。"""
        canonical = self.resolve(name)
        if not self._depth_cache:
            for node in self.topological_order():
                parents = [p for p in self.parents(node) if p]
                self._depth_cache[node] = (
                    1 + max(self._depth_cache[p] for p in parents)) if parents else 0
        return self._depth_cache[canonical]

    def generation_count(self):
        """谱系覆盖的世代数（深度 0 起算）。"""
        if not self._animals:
            return 0
        return max(self.depth(node) for node in self._animals) + 1

    def topological_order(self):
        """按亲子边的 Kahn 拓扑序，同序内按 ID 排序保证确定性。"""
        indegree = {node: 0 for node in self._animals}
        children = {node: [] for node in self._animals}
        for edge in self.edges(include_superseded=False):
            indegree[edge['child']] += 1
            children[edge['parent']].append(edge['child'])
        ready = sorted(node for node, degree in indegree.items() if degree == 0)
        ordered = []
        while ready:
            node = ready.pop(0)
            ordered.append(node)
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(ordered) != len(self._animals):
            raise PedigreeCycleError('祖谱存在环，无法拓扑排序')
        return ordered

    def validate(self):
        """全量 DFS 校验：环、自交、孤悬边。"""
        colors = {node: 0 for node in self._animals}
        for root in sorted(self._animals):
            self._visit(root, colors)
        for edge in self.edges(include_superseded=False):
            if edge['child'] not in self._animals or edge['parent'] not in self._animals:
                raise PedigreeValidationError(f"边 {edge} 引用了未登记个体")
            record = self._animals[edge['child']]
            if record['sire'] and record['dam'] and record['sire'] == record['dam']:
                raise PedigreeValidationError(
                    f"{edge['child']} 的父本与母本是同一个体")
        return True

    def _visit(self, node, colors):
        colors[node] = 1
        for parent in (p for p in self.parents(node) if p):
            if colors[parent] == 1:
                raise PedigreeCycleError(f'祖谱环经过 {node} 与 {parent}')
            if colors[parent] == 0:
                self._visit(parent, colors)
        colors[node] = 2

    # ---------------------------------------------------------------- 更正
    def replace_parent(self, child, role, new_parent, evidence_id, rationale=None):
        """亲权更正：旧边标记 superseded 并保留证据，新建活跃边。"""
        if role not in ('sire', 'dam'):
            raise PedigreeValidationError('role 只能是 sire 或 dam')
        child = self.resolve(child)
        new_parent = self.resolve(new_parent)
        record = self._animals[child]
        old_parent = record[role]
        if old_parent is None:
            raise PedigreeValidationError(f'{child} 没有可更正的 {role} 边')
        if old_parent == new_parent:
            return False
        if new_parent == child:
            raise PedigreeCycleError(f'{child} 不能成为自己的亲代')
        other_role = 'dam' if role == 'sire' else 'sire'
        if record[other_role] == new_parent:
            raise PedigreeValidationError(
                f'{child} 的父母不能是同一个体 {new_parent}')
        if child in self.ancestors(new_parent):
            raise PedigreeCycleError(
                f'把 {child} 的 {role} 改为 {new_parent} 会形成祖谱环')

        old_edge = self._edges[(child, old_parent)]
        old_edge['status'] = 'superseded'
        old_edge['superseded_by'] = evidence_id
        if evidence_id and evidence_id not in old_edge['evidence_ids']:
            old_edge['evidence_ids'].append(evidence_id)

        record[role] = new_parent
        self._edges[(child, new_parent)] = {
            'child': child, 'parent': new_parent, 'role': role,
            'status': 'active',
            'evidence_ids': [evidence_id] if evidence_id else [],
            'replaces': old_parent, 'rationale': rationale,
        }
        if evidence_id:
            self._evidence.add(evidence_id)
            record['evidence_ids'] = record['evidence_ids'] + (evidence_id,)
        self._depth_cache.clear()
        self.revision += 1
        self.validate()
        return True

    # ---------------------------------------------------------------- 快照
    def snapshot(self, snapshot_id, subject, version, rationale='',
                 parent_snapshot_id=None, evidence_ids=()):
        return {
            'snapshot_id': snapshot_id,
            'subject': subject,
            'version': version,
            'rationale': rationale,
            'parent_snapshot_id': parent_snapshot_id,
            'evidence_ids': sorted(set(evidence_ids)),
            'animals': [
                {
                    'id': record['id'],
                    'sire': record['sire'],
                    'dam': record['dam'],
                    'aliases': list(record['aliases']),
                    'born': record['born'],
                    'evidence_ids': list(record['evidence_ids']),
                }
                for record in (self._animals[node]
                               for node in self.topological_order())
            ],
            'edges': [
                {
                    'child': edge['child'],
                    'parent': edge['parent'],
                    'role': edge['role'],
                    'status': edge['status'],
                    'evidence_ids': list(edge['evidence_ids']),
                    'superseded_by': edge.get('superseded_by'),
                    'replaces': edge.get('replaces'),
                    'rationale': edge.get('rationale'),
                }
                for edge in self.edges(include_superseded=True)
            ],
        }

    @classmethod
    def from_snapshot(cls, data):
        book = cls()
        # 快照中的 animals 已按拓扑序写入；保险起见按边关系再排一次。
        pending = {rec['id']: rec for rec in data['animals']}
        placed = set()
        while pending:
            progressed = False
            for animal_id in sorted(pending):
                rec = pending[animal_id]
                parents = [p for p in (rec['sire'], rec['dam']) if p]
                if all(p in placed for p in parents):
                    book.add_animal(
                        rec['id'], rec['sire'], rec['dam'],
                        rec.get('aliases', ()), rec.get('born'),
                        rec['evidence_ids'][0] if rec.get('evidence_ids') else None)
                    if len(rec.get('evidence_ids', ())) > 1:
                        book._animals[animal_id]['evidence_ids'] = tuple(rec['evidence_ids'])
                    placed.add(animal_id)
                    pending.pop(animal_id)
                    progressed = True
            if not progressed:
                raise PedigreeCycleError('快照无法按拓扑序重建，疑似含环')
        for edge in data['edges']:
            key = (edge['child'], edge['parent'])
            if edge['status'] == 'active':
                stored = book._edges[key]
                stored['evidence_ids'] = list(edge['evidence_ids'])
            else:
                book._edges[key] = {
                    'child': edge['child'], 'parent': edge['parent'],
                    'role': edge['role'], 'status': 'superseded',
                    'evidence_ids': list(edge['evidence_ids']),
                    'superseded_by': edge.get('superseded_by'),
                    'replaces': edge.get('replaces'),
                    'rationale': edge.get('rationale'),
                }
        book.validate()
        return book

    # ---------------------------------------------------------------- 事件
    def load_events(self, events):
        """向本册追加消费 pedigree 事件；对同一册重复导入完全幂等。"""
        added = skipped = 0
        pedigree_events = sorted(
            (event for event in events if event.get('kind') == 'pedigree'),
            key=lambda event: (event['event_time'], event['id']))
        for event in pedigree_events:
            data = event.get('data', {})
            changed = self.add_animal(
                data['animal_id'], data.get('sire'), data.get('dam'),
                data.get('aliases', ()), data.get('born'), event['id'])
            if changed:
                added += 1
            else:
                skipped += 1
        return {'added': added, 'skipped': skipped,
                'evidence_ids': sorted(self._evidence)}

    @classmethod
    def from_events(cls, events):
        """按 (event_time, id) 排序消费 pedigree 事件，返回 (谱系册, 计数)。

        同一批事件重复导入是幂等的：相同登记返回 skipped，冲突登记抛错。
        """
        book = cls()
        return book, book.load_events(events)
