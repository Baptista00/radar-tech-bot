"""Cofre OAuth local: DPAPI por usuario no Windows; permissoes privadas no POSIX."""

import ctypes
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from runtime_lock import instance_lock
from safety import SafeError

DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "meli_oauth"
WINDOWS_HEADER = b"RADAR-MELI-1-DPAPI\n"
POSIX_HEADER = b"RADAR-MELI-1-POSIX\n"


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Nao usa LOCAL_MACHINE: somente a conta Windows atual pode abrir o cofre."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; sem prompts e sem escopo de maquina.
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise SafeError("OAuth: protecao local do Windows indisponivel; nenhum segredo sera salvo em texto aberto.")
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


class LocalOAuthStore:
    """Usar load/save dentro de locked(); gravacoes sao atomicas, sem backups de tokens."""

    def __init__(self, directory: Path = DEFAULT_DIRECTORY):
        self.directory = directory.absolute()
        self.path = self.directory / "credentials.bin"
        self.lock_path = self.directory / "credentials.lock"

    def _check_paths(self) -> None:
        for path in (self.path, self.lock_path, self.directory, *self.directory.parents):
            if path.is_symlink() or path.is_junction():
                raise SafeError("OAuth: cofre nao pode usar links ou pastas redirecionadas.")
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise SafeError("OAuth: arquivo de credenciais precisa ser exclusivo.")

    def _prepare(self) -> None:
        self._check_paths()
        # DPAPI protege o conteudo no Windows, sem depender do ACL herdado.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o755 if os.name == "nt" else 0o700)
        if os.name != "nt":
            for path in (self.directory, self.path):
                if path.exists():
                    info = path.stat()
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                        raise SafeError("OAuth: cofre exige diretorio 700 e arquivo 600 pertencentes ao usuario atual.")

    @contextmanager
    def locked(self) -> Iterator[None]:
        try:
            self._prepare()
            with instance_lock(self.lock_path):
                yield
        except OSError:
            raise SafeError("OAuth: nao foi possivel acessar ou gravar o cofre local.") from None

    def load(self) -> dict[str, Any]:
        self._check_paths()
        if not self.path.exists():
            return {"version": 1}
        if self.path.stat().st_size > 262144:
            raise SafeError("OAuth: cofre local invalido; conteudo omitido.")
        data = self.path.read_bytes()
        expected = WINDOWS_HEADER if os.name == "nt" else POSIX_HEADER
        if not data.startswith(expected):
            raise SafeError("OAuth: formato ou plataforma do cofre local incompativel.")
        payload = data[len(expected):]
        if os.name == "nt":
            payload = _dpapi(payload, decrypt=True)
        try:
            state = json.loads(payload)
        except (ValueError, UnicodeError):
            raise SafeError("OAuth: cofre local invalido; conteudo omitido.") from None
        if not isinstance(state, dict) or state.get("version") != 1:
            raise SafeError("OAuth: versao ou formato do cofre local invalido.")
        return state

    def save(self, state: dict[str, Any]) -> None:
        self._check_paths()
        raw = json.dumps(state, ensure_ascii=True, allow_nan=False).encode("utf-8")
        payload = WINDOWS_HEADER + _dpapi(raw) if os.name == "nt" else POSIX_HEADER + raw
        temporary = self.directory / (".credentials-" + secrets.token_hex(16) + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
