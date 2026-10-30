import copy
import threading

import geo

geo.fetch = lambda text: None


class Snapshot:
    def __init__(self, path, value):
        self.id = path.rsplit('/', 1)[-1]
        self.exists = value is not None
        self.value = copy.deepcopy(value)

    def to_dict(self):
        return copy.deepcopy(self.value)


def reject_nested_arrays(value, inside=False):
    if isinstance(value, dict):
        for item in value.values():
            reject_nested_arrays(item)
    elif isinstance(value, (list, tuple)):
        if inside:
            raise ValueError('400 Property contains an invalid nested entity.')
        for item in value:
            reject_nested_arrays(item, True)


class Reference:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def get(self, transaction=None):
        if transaction and transaction.writes:
            raise AssertionError('Firestore requires all reads before writes')
        return Snapshot(self.path, self.db.values.get(self.path))

    def delete(self):
        self.db.values.pop(self.path, None)

    def set(self, value):
        self.db.values[self.path] = copy.deepcopy(value)


class Transaction:
    def __init__(self):
        self.writes = {}

    def set(self, reference, value):
        reject_nested_arrays(value)
        self.writes[reference.path] = copy.deepcopy(value)

    def delete(self, reference):
        self.writes[reference.path] = None


class Collection:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def where(self, field, operator, value):
        query = Collection(self.db, self.path)
        query.predicate = lambda row: row.get(field) in value if operator == 'in' else (row.get(field) or 0) >= value if operator == '>=' else row.get(field) == value
        return query

    def limit(self, count):
        return self

    def stream(self):
        prefix = self.path + '/'
        return iter([Snapshot(path, value) for path, value in list(self.db.values.items()) if path.startswith(prefix) and '/' not in path[len(prefix):] and getattr(self, 'predicate', lambda row: True)(value)])


class FakeFirestore:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def document(self, path):
        return Reference(self, path)

    def collection(self, path):
        return Collection(self, path)

    def get_all(self, references):
        return [reference.get() for reference in references]

    def run_transaction(self, operation):
        with self.lock:
            transaction = Transaction()
            result = operation(transaction)
            for path, value in transaction.writes.items():
                if value is None:
                    self.values.pop(path, None)
                else:
                    self.values[path] = value
            return result
