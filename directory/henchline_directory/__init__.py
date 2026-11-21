"""The Henchline directory: which hosts exist and when each was last seen, for paired phones.

Discovery only. It holds host-signed descriptors and observation times, never chats, files, pairing nonces
in clear, push addresses or any phone's private material, and it cannot vouch for a host: the phone checks
every descriptor against the host key it pinned when it paired with that host itself.
"""

__version__ = "1.0.0"
