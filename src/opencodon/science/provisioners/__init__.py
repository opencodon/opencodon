"""Kernel provisioners — where a science kernel runs.

``science.kernels`` defines the seam and the local implementation; this package
holds the backends that put a kernel somewhere else. Each is imported lazily so
an install without the backend's SDK still loads the science layer.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from opencodon.science.provisioners.modal_backend import ModalProvisioner
    from opencodon.science.provisioners.ssh_backend import SSHProvisioner

__all__ = ["ModalProvisioner", "SSHProvisioner", "get_provisioner"]


def __getattr__(name):
    if name == "ModalProvisioner":
        from opencodon.science.provisioners.modal_backend import ModalProvisioner

        return ModalProvisioner
    if name == "SSHProvisioner":
        from opencodon.science.provisioners.ssh_backend import SSHProvisioner

        return SSHProvisioner
    raise AttributeError(name)


def get_provisioner(target: str = "local", **kwargs):
    """Resolve a provisioner by name.

    ``local`` (default), ``modal`` — optionally with a GPU, e.g.
    ``get_provisioner("modal", gpu="A100")`` — or ``ssh``, e.g.
    ``get_provisioner("ssh", host="gpu-01", user="ada")``.
    """
    target = (target or "local").strip().lower()
    if target == "local":
        from opencodon.science.kernels import LocalProvisioner

        return LocalProvisioner()
    if target == "modal":
        from opencodon.science.provisioners.modal_backend import ModalProvisioner

        return ModalProvisioner(**kwargs)
    if target == "ssh":
        from opencodon.science.provisioners.ssh_backend import SSHProvisioner

        return SSHProvisioner(**kwargs)
    raise ValueError(
        f"unknown kernel target {target!r}; expected 'local', 'modal' or 'ssh'"
    )
