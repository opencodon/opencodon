"""Kernel provisioners — where a science kernel runs.

``science.kernels`` defines the seam and the local implementation; this package
holds the backends that put a kernel somewhere else. Each is imported lazily so
an install without the backend's SDK still loads the science layer.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from science.provisioners.modal_backend import ModalProvisioner

__all__ = ["ModalProvisioner", "get_provisioner"]


def __getattr__(name):
    if name == "ModalProvisioner":
        from science.provisioners.modal_backend import ModalProvisioner

        return ModalProvisioner
    raise AttributeError(name)


def get_provisioner(target: str = "local", **kwargs):
    """Resolve a provisioner by name.

    ``local`` (default), or ``modal`` — optionally with a GPU, e.g.
    ``get_provisioner("modal", gpu="A100")``.
    """
    target = (target or "local").strip().lower()
    if target == "local":
        from science.kernels import LocalProvisioner

        return LocalProvisioner()
    if target == "modal":
        from science.provisioners.modal_backend import ModalProvisioner

        return ModalProvisioner(**kwargs)
    raise ValueError(f"unknown kernel target {target!r}; expected 'local' or 'modal'")
