"""围栏报警传感器行解析。

本模块保持独立：只依赖标准数据结构，不接触存储、签名或工单逻辑，
方便在离线补传场景下单独复用与测试。
"""


def tripped_loops(readings):
    """从报警行取回路号。

    仅采纳 ``alarm`` 为真且传感器自检通过（``self_check`` 未显式为
    ``False``）的行；重复报警去重，结果按回路号排序，保证下游诊断
    确定且幂等。
    """
    tripped = []
    for row in readings:
        if not isinstance(row, dict) or 'loop' not in row or 'alarm' not in row:
            raise ValueError('围栏传感器行缺少 loop/alarm 字段')
        if not row['alarm'] or row.get('self_check', True) is False:
            continue
        if row['loop'] not in tripped:
            tripped.append(row['loop'])
    return sorted(tripped)


def failed_self_checks(readings):
    """返回自检失败的传感器回路；诊断前该列表必须为空。"""
    failed = []
    for row in readings:
        if isinstance(row, dict) and row.get('self_check', True) is False:
            loop = row.get('loop')
            if loop is not None and loop not in failed:
                failed.append(loop)
    return sorted(failed)
