# Upstream provenance

This is an independently maintained public Agent Edition repository.

Media extraction, quality selection, download scheduling, FFmpeg helpers, media verification and Cookie domain handling are derived from [OmniDownloader](https://github.com/baishu136/OmniDownloader), Copyright (c) 2026 OmniDownloader Contributors, under the MIT License preserved in LICENSE.

The windows/ source snapshot was taken from the working OmniDownloader source on 2026-11-17. The upstream main reference at review time was 62f44ac; the development workspace also contained newer X/Pixiv safety fixes. upstream-file-hashes.json records the exact imported files, rather than claiming they all belong to an upstream release tag.

windows/core and windows/engine are shared download implementation; windows/omni_server.py is retained for the upstream API regression suite. It is not the Agent Edition server entry. Desktop launchers, the original full GUI, Android and iOS applications are not shipped in this repository.

Agent Edition service, delivery ledger, security boundary, official MCP integration, bot adapters, management panel, packaging and examples live in agent-edition/. Its versions, CI, tags and release assets are managed here independently.

Native runtime provenance and hashes are in windows/runtime-lock.json. License texts for redistributed FFmpeg, Deno and the upstream application icon are retained in windows/licenses/ and copied into portable packages.
