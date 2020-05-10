"""
Exposure Notification reference implementation.
"""

import os
import struct
from datetime import datetime
from collections import OrderedDict
from binascii import hexlify

from Crypto.Cipher import AES
from Crypto.Util import Counter

from cryptography.hazmat.primitives import hashes

from .utils import hkdf_derive


"""
The TEKRollingPeriod is the duration for which a Temporary Exposure Key
is valid (in multiples of 10 minutes). In our protocol, TEKRollingPeriod
is defined as 144, achieving a key validity of 24 hours.
"""
SECONDS_PER_INTERVAL = 600
TEK_ROLLING_PERIOD = 144
TEK_LIFETIME = 14
BYTES_RPIK_INFO = "EN-RPIK".encode("utf-8")
BYTES_RPI = "EN-RPI".encode("utf-8")
BYTES_MID_PAD = b"\x00\x00\x00\x00\x00\x00"
BYTES_AEMK_INFO = "EN-AEMK".encode("utf-8")

_temporary_exposure_keys = OrderedDict()
_rolling_proximity_id_keys = OrderedDict()
_associated_enc_metadata_keys = OrderedDict()


def _interval_number_impl(time_at_key_gen: datetime) -> int:
    """
    Implements the function ENIntervalNumber in specification.
    """
    timestamp = int(time_at_key_gen.timestamp())
    return timestamp // SECONDS_PER_INTERVAL


def interval_number() -> int:
    """
    ENIntervalNumber of the present timestamp.
    This function provides a number for each 10 minute time window that’s
    shared between all devices participating in the protocol. These time
    windows are derived from timestamps in Unix Epoch Time.
    """
    return _interval_number_impl(datetime.utcnow())


def temporary_exposure_key() -> bytes:
    """
    Generates Temporary Exposure Key once for each TEKRollingPeriod (day).
    Generation is done once a day and calculation is amortized.
    """
    global _temporary_exposure_keys
    curr_interval_num = interval_number()
    curr_interval_day = curr_interval_num // TEK_ROLLING_PERIOD

    if curr_interval_day not in _temporary_exposure_keys:
        _temporary_exposure_keys[curr_interval_day] = os.urandom(16)
        temp_dict = OrderedDict(
            {
                prev_key: _temporary_exposure_keys[prev_key]
                for prev_key in _temporary_exposure_keys
                if curr_interval_day - prev_key <= TEK_LIFETIME
            }
        )
        _temporary_exposure_keys = temp_dict

    return _temporary_exposure_keys[curr_interval_day]


def _rolling_key_deriv(container, info) -> bytes:
    """Implements HKDF with caching"""
    curr_interval_num = interval_number()
    curr_interval_day = curr_interval_num // TEK_ROLLING_PERIOD

    if curr_interval_day not in container:
        curr_key = hkdf_derive(
            input_key=temporary_exposure_key(),
            salt=b"",
            info=info,
            length=16,
            hash_algo=hashes.SHA256(),
        )
        container[curr_interval_day] = curr_key

        # Clean up old keys
        temp_dict = OrderedDict(
            {
                prev_key: container[prev_key]
                for prev_key in container
                if curr_interval_day - prev_key <= TEK_LIFETIME
            }
        )
        container = temp_dict

    return container[curr_interval_day]


def rolling_proximity_identifier_key() -> bytes:
    """
    The Rolling Proximity Identifier Key (RPIK) is derived from the
    Temporary Exposure Key and is used in order to derive the
    Rolling Proximity Identifiers.
    Generates RPIK once every given TEKRollingPeriod (1 day).
    """
    global _rolling_proximity_id_keys
    return _rolling_key_deriv(_rolling_proximity_id_keys, BYTES_RPIK_INFO)


def rolling_proximity_identifier() -> bytes:
    """
    Rolling Proximity Identifiers are privacy-preserving identifiers that are
    broadcast in Bluetooth payloads. Each time the Bluetooth Low Energy MAC
    randomized address changes, we derive a new Rolling Proximity Identifier
    using the Rolling Proximity Identifier Key.
    ENIntervalNumber is encoded as a 32-bit (uint32_t) unsigned little-endian
    value.
    """
    curr_interval_num = interval_number()
    curr_rpik = rolling_proximity_identifier_key()
    padded_data = (
        BYTES_RPI + BYTES_MID_PAD + struct.pack("<I", curr_interval_num)
    )
    cipher = AES.new(key=curr_rpik, mode=AES.MODE_ECB)
    return cipher.encrypt(padded_data)


def associated_encrypted_metadata_key() -> bytes:
    """
    The Associated Encrypted Metadata Keys are derived from the
    Temporary Exposure Keys in order to encrypt additional metadata.
    """
    global _associated_enc_metadata_keys
    return _rolling_key_deriv(_associated_enc_metadata_keys, BYTES_AEMK_INFO)


def associated_encrypted_metadata(metadata: bytes) -> bytes:
    """
    The Associated Encrypted Metadata is data encrypted along with the
    Rolling Proximity Identifier, and can only be decrypted later if the user
    broadcasting it tested positive and reveals their Temporary Exposure Key.
    The 16-byte Rolling Proximity Identifier and the appended encrypted
    metadata are broadcast over Bluetooth Low Energy wireless technology.
    """
    curr_aemk = associated_encrypted_metadata_key()
    curr_rpi = rolling_proximity_identifier()
    curr_rpi_hex = hexlify(curr_rpi)
    curr_rpi_int = int(curr_rpi_hex, 16)
    counter = Counter.new(nbits=128, initial_value=curr_rpi_int)
    cipher = AES.new(key=curr_aemk, mode=AES.MODE_CTR, counter=counter)
    return cipher.encrypt(metadata)
