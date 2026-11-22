from .studbook import ancestor_ids

def shared_parent_count(animals, first, second):
    return len(set(ancestor_ids(animals, first)) & set(ancestor_ids(animals, second)))
