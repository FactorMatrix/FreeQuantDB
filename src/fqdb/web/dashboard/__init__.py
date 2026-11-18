"""Read-only view registry for the local console."""

from functools import partial

from ...util import FQDBError
from .catalog import CatalogViews
from .operations import OperationViews


class UnknownView(FQDBError):
    pass


class Dashboard:
    def __init__(self, owner):
        self.datasets = CatalogViews(owner)
        self.operations = OperationViews(owner)
        self._views = {
            "summary": partial(self.datasets.summary, self.operations.runtime),
            "browse": self.datasets.browse,
            "dataset": self.datasets.dataset,
            "package": self.datasets.package,
            "audit": self.datasets.audit,
            "runtime": self.operations.runtime,
            "tasks": self.operations.tasks,
            "features": self.operations.features,
            "layout": self.operations.layout,
            "publisher_options": self.operations.publisher_options,
        }

    def view(self, name, params):
        handler = self._views.get(name)
        if handler is None:
            raise UnknownView("Unknown console view")
        return handler(**params)
