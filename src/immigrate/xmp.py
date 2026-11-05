"""Build an XMP sidecar carrying what Apple Photos keeps outside the image file."""

from __future__ import annotations

from xml.sax.saxutils import escape

from .library import Item

_HEADER = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '<rdf:Description rdf:about=""\n'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
    ' xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
    ' xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
    ' xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"\n'
    ' xmlns:digiKam="http://www.digikam.org/ns/1.0/">\n'
)
_FOOTER = '</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end="w"?>\n'


def _gps(value: float, positive: str, negative: str) -> str:
    """XMP GPS format: degrees,decimal-minutes followed by a hemisphere letter."""
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60
    return f"{degrees},{minutes:.6f}{positive if value >= 0 else negative}"


def _alt(tag: str, text: str) -> str:
    return (
        f'<{tag}><rdf:Alt><rdf:li xml:lang="x-default">{escape(text)}</rdf:li></rdf:Alt></{tag}>\n'
    )


def _bag(tag: str, values: list[str]) -> str:
    entries = "".join(f"<rdf:li>{escape(value)}</rdf:li>" for value in values)
    return f"<{tag}><rdf:Bag>{entries}</rdf:Bag></{tag}>\n"


def tags_for(item: Item, people_as_tags: bool) -> list[str]:
    tags = list(item.keywords)
    if people_as_tags:
        tags.extend(f"People/{name}" for name in item.people)
    return tags


def build_sidecar(item: Item, people_as_tags: bool = True) -> bytes:
    stamp = item.taken.isoformat(timespec="seconds")
    body = [
        f"<exif:DateTimeOriginal>{stamp}</exif:DateTimeOriginal>\n",
        f"<xmp:CreateDate>{stamp}</xmp:CreateDate>\n",
        f"<photoshop:DateCreated>{stamp}</photoshop:DateCreated>\n",
    ]
    if item.title:
        body.append(_alt("dc:title", item.title))
    if item.description:
        body.append(_alt("dc:description", item.description))
    if item.latitude is not None and item.longitude is not None:
        body.append(f"<exif:GPSLatitude>{_gps(item.latitude, 'N', 'S')}</exif:GPSLatitude>\n")
        body.append(f"<exif:GPSLongitude>{_gps(item.longitude, 'E', 'W')}</exif:GPSLongitude>\n")
    if item.favorite:
        body.append("<xmp:Rating>5</xmp:Rating>\n")
    tags = tags_for(item, people_as_tags)
    if tags:
        body.append(_bag("dc:subject", tags))
        body.append(_bag("digiKam:TagsList", tags))
    return (_HEADER + "".join(body) + _FOOTER).encode("utf-8")
