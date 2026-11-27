"""Nested transaction scopes for store operations sharing one serialized connection."""


class NestedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.depth = 0
        self.failed = False
        self._start_changes = 0
        self.mutation_seq = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        if self.depth == 0:
            self.failed = False
            self._start_changes = int(getattr(self.connection, "total_changes", 0) or 0)
        self.depth += 1
        return self

    def __exit__(self, kind, value, traceback):
        self.failed = self.failed or kind is not None
        self.depth -= 1
        if self.depth == 0:
            if self.failed:
                self.connection.rollback()
            else:
                self.connection.commit()
                end_changes = int(getattr(self.connection, "total_changes", 0) or 0)
                if end_changes > self._start_changes:
                    self.mutation_seq += 1
        return False
