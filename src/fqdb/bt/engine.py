from __future__ import annotations

from ..layout import runtime_path

import logging
from pathlib import Path
import time

import libtorrent as lt

from .torrents import (
    MAX_METADATA,
    inspect,
    magnet_hash,
    metadata_torrent,
    torrent_cache_path,
    validate_daily,
    validate_info,
)
from ..util import FQDBError, atomic_bytes

log = logging.getLogger(__name__)
PEX_ENCRYPTION = 1


class Engine:
    """One embedded native libtorrent session, owned exclusively by the data-root service."""

    def __init__(self, root, settings):
        self.root = root
        self.discovery = settings["discovery"]
        self.session = lt.session(
            {
                "listen_interfaces": settings["listen"],
                "enable_dht": settings["discovery"],
                "enable_lsd": settings["discovery"],
                "enable_upnp": False,
                "enable_natpmp": False,
                "allow_multiple_connections_per_ip": True,
                "ignore_limits_on_local_network": False,
                # A peer can disconnect after serving metadata. Keep native
                # reconnects within the explicit peer loop's timeout window.
                "min_reconnect_time": 1,
                "connections_limit": settings["connections_limit"],
                "upload_rate_limit": settings["upload_limit"],
                "download_rate_limit": settings["download_limit"],
                "max_metadata_size": MAX_METADATA,
                "alert_mask": int(
                    lt.alert.category_t.error_notification
                    | lt.alert.category_t.storage_notification
                    | lt.alert.category_t.status_notification
                ),
            }
        )
        self.handles = {}
        self.uploads = set()
        self.moves = {}
        self.resume_seeds = set()
        self.saved_at = 0
        self.errors = {}
        self.metadata_pending = set()
        self.metadata_audit = {}

    def prepare(self, params):
        params.flags &= ~(
            lt.torrent_flags.auto_managed | lt.torrent_flags.paused | lt.torrent_flags.seed_mode
        )
        # A trusted catalog authorizes BT, never an arbitrary HTTP web seed or a
        # peer supplied in a magnet. Explicit configured peers are connected later.
        params.url_seeds = []
        params.http_seeds = []
        params.peers = []
        params.flags |= lt.torrent_flags.override_web_seeds
        discovery_flags = (
            lt.torrent_flags.override_trackers
            | lt.torrent_flags.disable_dht
            | lt.torrent_flags.disable_lsd
            | lt.torrent_flags.disable_pex
        )
        if self.discovery:
            params.flags &= ~discovery_flags
        else:
            params.trackers = []
            params.tracker_tiers = []
            params.flags |= discovery_flags
        return params

    def add(self, identity, torrent_path, save_path, recheck=False, upload_only=False, magnet=None):
        if identity in self.handles:
            return self.handles[identity]
        info = inspect(Path(torrent_path).read_bytes(), self.root)
        if str(info.info_hashes().v1) != identity:
            raise FQDBError("Cached metadata does not match the original info-hash")
        resume = runtime_path(self.root, "state") / f"{identity}.fastresume"
        params = lt.add_torrent_params()
        if not recheck and resume.exists():
            try:
                saved = lt.read_resume_data(resume.read_bytes())
                if str(saved.info_hashes.v1) == identity and Path(saved.save_path) == Path(save_path):
                    params = saved
            except Exception:
                log.exception("Ignoring invalid resume metadata: %s", identity)
        params.ti = info
        params.save_path = str(save_path)
        if magnet:
            if magnet_hash(magnet) != identity:
                raise FQDBError("Magnet does not match the verified metadata")
            advertised = list(lt.parse_magnet_uri(magnet).trackers)
            if advertised:
                trackers = {tracker.url: tracker.tier for tracker in info.trackers()}
                for url in advertised:
                    trackers.setdefault(url, 0)
                params.trackers = list(trackers)
                params.tracker_tiers = list(trackers.values())
        # The trusted metadata names the only file we are allowed to install.
        if hasattr(params, "renamed_files"):
            params.renamed_files = {}
        self.prepare(params)
        params.flags &= ~(lt.torrent_flags.upload_mode | lt.torrent_flags.default_dont_download)
        if upload_only:
            params.flags |= lt.torrent_flags.upload_mode
        params.file_priorities = [4] * info.num_files()
        handle = self.session.add_torrent(params)
        self.handles[identity] = handle
        if upload_only:
            self.uploads.add(identity)
            if not recheck:
                self.resume_seeds.add(identity)
        if recheck:
            handle.force_recheck()
        return handle

    def add_magnet(self, identity, magnet, save_path):
        if magnet_hash(magnet) != identity:
            raise FQDBError("Magnet info-hash does not match trusted manifest")
        if identity in self.handles:
            return self.handles[identity]
        params = self.prepare(lt.parse_magnet_uri(magnet))
        params.save_path = str(save_path)
        # In libtorrent 2.0 upload_mode permits BEP-9 metadata exchange while
        # blocking piece requests. default_dont_download adds a second gate when
        # metadata is installed. Auto-management is disabled so it cannot lift it.
        params.flags |= lt.torrent_flags.upload_mode | lt.torrent_flags.default_dont_download
        handle = self.session.add_torrent(params)
        self.handles[identity] = handle
        self.metadata_pending.add(identity)
        self.metadata_audit[identity] = {"metadata_validated": False, "payload_before_validation": 0}
        return handle

    def accept_metadata(self, identity, entry):
        handle = self.handles[identity]
        status = handle.status()
        if not status.has_metadata:
            return False
        downloaded = max(status.total_payload_download, status.all_time_download)
        self.metadata_audit[identity]["payload_before_validation"] = downloaded
        if downloaded:
            raise FQDBError("Payload arrived before metadata validation")
        info = validate_info(handle.torrent_file(), entry, self.root)
        # Cache only validated metadata. Raw info bytes keep the original swarm.
        target = torrent_cache_path(self.root, entry)
        try:
            atomic_bytes(target, metadata_torrent(info, handle.trackers()), immutable=True)
        except FileExistsError:
            # Another local operation may have cached the same info section
            # with different optional outer metadata.
            validate_daily(target.read_bytes(), entry, self.root)
        # Open the payload from the verified torrent. The metadata-only handle
        # can have closed peers and file priorities assigned before validation.
        self.remove(identity)
        self.add(identity, target, status.save_path, magnet=entry.magnet)
        self.metadata_audit[identity].update(
            metadata_validated=True,
            metadata_bytes=len(info.info_section()),
            native_size=info.files().file_size(0),
        )
        return True

    def remove(self, identity):
        h = self.handles.pop(identity, None)
        if h and h.is_valid():
            if identity in self.resume_seeds and identity not in self.moves:
                h.save_resume_data(lt.torrent_handle.only_if_modified)
            self.session.remove_torrent(h)  # Never request delete_files.
        self.uploads.discard(identity)
        self.resume_seeds.discard(identity)
        self.metadata_pending.discard(identity)
        self._release_staging(identity)

    def promote(self, identity, source, final):
        """Keep the live torrent and point it at the installed local snapshot."""
        handle = self.handles[identity]
        # Normal activation installs a hard link to the already verified inode.
        # Other filesystems or an existing snapshot need native piece checking.
        flags = (
            lt.move_flags_t.reset_save_path_unchecked
            if source.samefile(final)
            else lt.move_flags_t.reset_save_path
        )
        stat = source.stat()
        handle.set_flags(lt.torrent_flags.upload_mode)
        handle.move_storage(str(final.parent), flags)
        self.moves[identity] = (source, stat.st_dev, stat.st_ino)
        self.uploads.add(identity)
        self.resume_seeds.add(identity)

    def _release_staging(self, identity):
        move = self.moves.pop(identity, None)
        if move is None:
            return
        source, device, inode = move
        try:
            stat = source.stat()
            if (stat.st_dev, stat.st_ino) == (device, inode):
                source.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not remove completed staging file for %s", identity, exc_info=True)

    def connect(self, peers):
        if not peers:
            return
        for identity, h in list(self.handles.items()):
            if identity not in self.uploads and h.is_valid() and not h.status().is_seeding:
                for host, port in peers:
                    # A configured endpoint supplies no uTP capability hint.
                    h.connect_peer((host, int(port)), 0, PEX_ENCRYPTION)

    def poll(self):
        for alert in self.session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                identity = str(alert.params.info_hashes.v1)
                atomic_bytes(
                    runtime_path(self.root, "state") / f"{identity}.fastresume",
                    bytes(lt.write_resume_data_buf(alert.params)),
                )
            elif isinstance(alert, lt.storage_moved_alert):
                identity = str(alert.handle.info_hashes().v1)
                if identity in self.moves and alert.handle == self.handles.get(identity):
                    self._release_staging(identity)
                    alert.handle.save_resume_data()
            elif isinstance(alert, (lt.torrent_error_alert, lt.file_error_alert, lt.storage_moved_failed_alert)):
                identity = str(alert.handle.info_hashes().v1)
                if alert.handle == self.handles.get(identity):
                    self.errors[identity] = alert.message()
                    log.error("%s", alert.message())
        if time.monotonic() - self.saved_at > 5:
            self.save_resume(lt.torrent_handle.only_if_modified)
            self.saved_at = time.monotonic()

    def save_resume(self, flags=0):
        for identity, h in list(self.handles.items()):
            if (identity not in self.uploads or identity in self.resume_seeds) and (
                identity not in self.metadata_pending and identity not in self.moves and h.is_valid()
            ):
                h.save_resume_data(flags)

    def close(self):
        self.save_resume()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.poll()
            time.sleep(0.05)
        self.session.pause()
        self.handles.clear()
        self.session = None
