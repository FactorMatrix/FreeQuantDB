"""Check original BT pieces on local synthetic bytes without peers."""

import hashlib

import libtorrent as lt

from fqdb.bt.torrents import verify_pieces


def test_native_piece_check_uses_original_geometry_and_accepts_a_local_filename(data_root):
    # An imported v1 torrent can have a piece length the creator would reject.
    # Keep the original geometry, including the last partial piece.
    piece_length = 12_288
    payload = b"SYNTHETIC-BT-VERIFICATION" * 2_000
    info = lt.torrent_info(
        lt.bencode(
            {
                b"info": {
                    b"name": b"SYNTHETIC-advertised.parquet",
                    b"length": len(payload),
                    b"piece length": piece_length,
                    b"pieces": b"".join(
                        hashlib.sha1(payload[i : i + piece_length]).digest()
                        for i in range(0, len(payload), piece_length)
                    ),
                }
            }
        )
    )
    local = data_root / "SYNTHETIC-local.parquet"
    local.write_bytes(payload)
    assert verify_pieces(info, local)
    assert local.read_bytes() == payload
    assert info.files().file_path(0) == "SYNTHETIC-advertised.parquet"
    assert not (data_root / "SYNTHETIC-advertised.parquet").exists()
    assert not (data_root / ".state/state/service.json").exists()

    local.write_bytes(payload[:-1] + b"!")
    assert not verify_pieces(info, local)
    local.write_bytes(payload + b"!")
    assert not verify_pieces(info, local)
