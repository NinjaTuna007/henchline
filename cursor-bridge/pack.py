"""Packs the bridge as a .vsix (a zip with a manifest) without needing Node's vsce: `python3 pack.py OUT.vsix`.
`python3 pack.py OUT.vsix reload` packs the reload helper (cursor-bridge/reload/) instead."""

import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if len(sys.argv) > 2:
    HERE = HERE / sys.argv[2]
meta = json.loads((HERE / "package.json").read_text())
manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{meta['name']}" Version="{meta['version']}" Publisher="{meta['publisher']}" />
    <DisplayName>{meta['displayName']}</DisplayName>
    <Description xml:space="preserve">{meta['description']}</Description>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{meta['engines']['vscode']}" />
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code" /></Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
  </Assets>
</PackageManifest>
"""
types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json" /><Default Extension=".js" ContentType="application/javascript" />
  <Default Extension=".md" ContentType="text/markdown" /><Default Extension=".vsixmanifest" ContentType="text/xml" />
</Types>
"""
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as vsix:
    vsix.writestr("[Content_Types].xml", types)
    vsix.writestr("extension.vsixmanifest", manifest)
    for name in ("package.json", "extension.js", "README.md"):
        if (HERE / name).exists():
            vsix.write(HERE / name, f"extension/{name}")
