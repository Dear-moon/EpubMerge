"""Post-merge EPUB fixer for EpubMerge.

Fixes common issues in merged EPUBs:
1. Duplicate/missing/invalid DOCTYPE declarations in HTML files
2. SVG-wrapped images (<svg><image xlink:href/></svg>) converted to <img>
   (fixes Kindle AZW3 blank page issue with SVG-embedded images)
3. Corrupt XML declarations from buggy upstream tools
4. Missing xml:lang attributes on <html> tags
5. Non-standard OPF media types (origrootfile/xml, origtocncx/xml)
6. Non-existent image references in rename map (pre-existing broken refs)
7. Rename images sequentially (1.jpg, 2.jpg, ...) by spine order,
   updating all HTML and OPF references accordingly.
"""

import os
import re
import shutil
import zipfile
from collections import OrderedDict
from xml.etree import ElementTree as ET


class EpubFixer:
    """Post-process an EPUB to fix HTML structure and normalize image references."""

    def __init__(self, input_path, output_path=None):
        self.input_path = input_path
        if output_path is None:
            base, ext = os.path.splitext(input_path)
            self.output_path = f"{base}_fixed{ext}"
        else:
            self.output_path = output_path

        # {old_zip_path: new_filename (e.g. "42.jpg")}
        self.image_rename_map = OrderedDict()
        self.image_counter = 0
        # {path: bytes} for all ZIP entries
        self.zip_entries = {}
        self.file_list = []

        self.ns = {
            'opf': 'http://www.idpf.org/2007/opf',
            'dc': 'http://purl.org/dc/elements/1.1/',
            'xhtml': 'http://www.w3.org/1999/xhtml',
            'svg': 'http://www.w3.org/2000/svg',
            'xlink': 'http://www.w3.org/1999/xlink',
            'container': 'urn:oasis:names:tc:opendocument:xmlns:container',
        }
        self._register_namespaces()

    def _register_namespaces(self):
        for prefix, uri in self.ns.items():
            ET.register_namespace(prefix, uri)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self):
        self._read_epub()
        self._parse_container()
        spine_order, _manifest = self._parse_root_opf()
        self._collect_image_order(spine_order)
        self._fix_html_files(spine_order)
        self._fix_root_opf()
        self._fix_sub_opfs()
        self._rename_images()
        self._write_epub()

    # ------------------------------------------------------------------
    # ZIP I/O
    # ------------------------------------------------------------------

    def _read_epub(self):
        with zipfile.ZipFile(self.input_path, 'r') as zf:
            self.file_list = zf.namelist()
            for name in self.file_list:
                self.zip_entries[name] = zf.read(name)

    def _write_epub(self):
        tmp_path = self.output_path + '.tmp'
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if 'mimetype' in self.zip_entries:
                zf.writestr('mimetype', self.zip_entries['mimetype'],
                            compress_type=zipfile.ZIP_STORED)
            for name in self.file_list:
                if name == 'mimetype':
                    continue
                data = self.zip_entries.get(name)
                if data is not None:
                    zf.writestr(name, data)
        if os.path.exists(self.output_path):
            os.remove(self.output_path)
        shutil.move(tmp_path, self.output_path)

    # ------------------------------------------------------------------
    # OPF parsing
    # ------------------------------------------------------------------

    def _parse_container(self):
        container_xml = self.zip_entries.get('META-INF/container.xml')
        if container_xml is None:
            raise ValueError("META-INF/container.xml not found")
        root = ET.fromstring(container_xml)
        ns = self.ns['container']
        rootfile = root.find(f'.//{{{ns}}}rootfile')
        if rootfile is None:
            rootfile = root.find('.//rootfile')
        if rootfile is None:
            raise ValueError("rootfile not found in container.xml")
        self.root_opf_path = rootfile.get('full-path')

    def _parse_root_opf(self):
        opf_data = self.zip_entries.get(self.root_opf_path)
        if opf_data is None:
            raise ValueError(f"Root OPF not found: {self.root_opf_path}")
        self.root_opf_xml = opf_data.decode('utf-8')
        self.opf_root = ET.fromstring(self.root_opf_xml)
        self.opf_base = os.path.dirname(self.root_opf_path)

        # Detect EPUB version for correct DOCTYPE selection
        pkg_ver = self.opf_root.get('version', '2.0')
        self.epub_version = float(pkg_ver) if pkg_ver else 2.0

        manifest = {}
        ns = self.ns['opf']
        manifest_elem = self.opf_root.find(f'{{{ns}}}manifest')
        if manifest_elem is None:
            manifest_elem = self.opf_root.find('manifest')

        if manifest_elem is not None:
            for item in manifest_elem:
                item_id = item.get('id')
                href = item.get('href')
                if item_id and href:
                    full_href = self._resolve_path(self.opf_base, href)
                    manifest[item_id] = {
                        'href': href,
                        'full_href': full_href,
                        'media_type': item.get('media-type', ''),
                        'element': item,
                    }

        spine_order = []
        spine_elem = self.opf_root.find(f'{{{ns}}}spine')
        if spine_elem is None:
            spine_elem = self.opf_root.find('spine')

        if spine_elem is not None:
            for itemref in spine_elem:
                idref = itemref.get('idref')
                if idref and idref in manifest:
                    spine_order.append({
                        'idref': idref,
                        'full_href': manifest[idref]['full_href'],
                        'element': itemref,
                    })

        self.manifest = manifest
        return spine_order, manifest

    def _resolve_path(self, base_dir, href):
        if base_dir and base_dir != '.' and base_dir != '':
            parts = base_dir.split('/') + href.split('/')
        else:
            parts = href.split('/')
        resolved = []
        for p in parts:
            if p == '.' or p == '':
                continue
            elif p == '..':
                if resolved:
                    resolved.pop()
            else:
                resolved.append(p)
        return '/'.join(resolved)

    # ------------------------------------------------------------------
    # Image collection (first pass: spine order)
    # ------------------------------------------------------------------

    def _collect_image_order(self, spine_order):
        seen_images = set()
        for entry in spine_order:
            full_href = entry['full_href']
            if not full_href.lower().endswith(('.xhtml', '.html', '.htm')):
                continue
            data = self.zip_entries.get(full_href)
            if data is None:
                continue
            content = self._decode_text(data)
            refs = self._extract_image_refs(content, full_href)
            for ref_path in refs:
                if ref_path not in seen_images:
                    seen_images.add(ref_path)
                    # Only rename images that actually exist in the archive
                    if ref_path not in self.zip_entries:
                        continue
                    ext = os.path.splitext(ref_path)[1].lower()
                    if not ext:
                        ext = '.jpg'
                    self.image_counter += 1
                    self.image_rename_map[ref_path] = f"{self.image_counter}{ext}"

    def _extract_image_refs(self, content, html_path):
        refs = []
        html_dir = os.path.dirname(html_path)
        # xlink:href in SVG
        for m in re.finditer(r'xlink:href="([^"]+)"', content):
            full = self._resolve_path(html_dir, m.group(1))
            if self._is_image_path(full):
                refs.append(full)
        # src in <img>
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', content):
            ref = m.group(1)
            if ref.startswith('http'):
                continue
            full = self._resolve_path(html_dir, ref)
            if self._is_image_path(full):
                refs.append(full)
        return refs

    def _is_image_path(self, path):
        return os.path.splitext(path)[1].lower() in (
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg')

    # ------------------------------------------------------------------
    # HTML fixing
    # ------------------------------------------------------------------

    def _fix_html_files(self, spine_order):
        processed = set()
        # First, fix files in spine order
        for entry in spine_order:
            full_href = entry['full_href']
            if not full_href.lower().endswith(('.xhtml', '.html', '.htm')):
                continue
            if full_href in processed:
                continue
            processed.add(full_href)
            data = self.zip_entries.get(full_href)
            if data is None:
                continue
            self.zip_entries[full_href] = self._fix_single_html(
                self._decode_text(data), full_href).encode('utf-8')
        # Then, fix any remaining HTML files not in spine
        for path in self.file_list:
            if path.lower().endswith(('.xhtml', '.html', '.htm')) and path not in processed:
                data = self.zip_entries.get(path)
                if data is None:
                    continue
                self.zip_entries[path] = self._fix_single_html(
                    self._decode_text(data), path).encode('utf-8')
                processed.add(path)

    def _fix_single_html(self, content, html_path):
        # Strip BOM characters (may appear at start or between DOCTYPE and <html>)
        content = content.replace(chr(0xFEFF), '')
        # Remove corrupt XML declarations from buggy tools
        content = self._remove_corrupt_xml_declarations(content)
        # Ensure exactly one correct DOCTYPE
        content = self._ensure_doctype(content)
        # Ensure html/head/body structure
        content = self._ensure_html_structure(content)
        # Ensure xml:lang attribute
        content = self._ensure_xml_lang(content)
        # Convert SVG-wrapped images to <img> (fixes Kindle rendering)
        content = self._convert_svg_to_img(content, html_path)
        return content

    def _remove_corrupt_xml_declarations(self, content):
        """Remove malformed XML PIs like <?Section0001.xhtmlxml ...?>."""
        corrupt_pi = r'<\?[^?]*?\.(?:xhtml|html|htm)[^?]*\?>[\r\n]*'
        corrupt_comment = r'<!--\s*\?[^>]*?\.(?:xhtml|html|htm).*?-->[\r\n]*'
        content = re.sub(corrupt_comment, '', content)
        content = re.sub(corrupt_pi, '', content)
        return content

    def _ensure_doctype(self, content):
        """Replace all DOCTYPEs with exactly one correct for the EPUB version."""
        doctype_pattern = r'<!DOCTYPE\s[^>]*>'
        all_doctypes = list(re.finditer(doctype_pattern, content[:800], re.IGNORECASE))

        if self.epub_version < 3.0:
            doctype = ('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
                       '  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n')
        else:
            doctype = '<!DOCTYPE html>\n'

        if not all_doctypes:
            xml_match = re.match(r'(\s*<\?xml[^?]*\?>\s*)', content)
            if xml_match:
                return xml_match.group(1) + doctype + content[xml_match.end():]
            else:
                return doctype + content

        # Remove all existing DOCTYPEs (processing backwards to preserve indices)
        for m in reversed(all_doctypes):
            start, end_ = m.start(), m.end()
            # Eat surrounding whitespace
            while start > 0 and content[start - 1] in (' ', '\t', '\r', '\n'):
                start -= 1
            while end_ < len(content) and content[end_] in ('\r', '\n'):
                end_ += 1
            content = content[:start] + content[end_:]

        xml_match = re.match(r'(\s*<\?xml[^?]*\?>\s*)', content)
        if xml_match:
            return xml_match.group(1) + doctype + content[xml_match.end():]
        else:
            return doctype + content

    def _ensure_html_structure(self, content):
        has_html_open = '<html' in content[:500]
        has_html_close = '</html>' in content[-200:]
        has_head = '<head' in content[:500]
        has_body = '<body' in content[:1000]

        if not has_head and has_html_open:
            content = re.sub(
                r'(<html[^>]*>)',
                r'\1\n<head>\n<meta http-equiv="Content-Type" '
                r'content="text/html; charset=utf-8"/>\n</head>',
                content, count=1)
        if not has_body and has_html_open:
            if '</head>' in content:
                idx = content.find('</head>')
                body_close = content.rfind('</html>')
                if body_close > idx:
                    inner = content[idx + len('</head>'):body_close]
                    content = (content[:idx + len('</head>')]
                               + '\n<body>' + inner + '</body>\n'
                               + content[body_close:])
            else:
                content = content.replace('</html>', '</body>\n</html>')
        if not has_html_close:
            content = content.rstrip() + '\n</html>'
        return content

    def _ensure_xml_lang(self, content):
        html_match = re.search(r'<html([^>]*)>', content[:300])
        if not html_match:
            return content
        attrs = html_match.group(1)
        if 'xml:lang' in attrs or 'lang=' in attrs:
            return content
        new_tag = '<html xml:lang="zh-CN"' + attrs + '>'
        return content[:html_match.start()] + new_tag + content[html_match.end():]

    def _convert_svg_to_img(self, content, html_path):
        """Replace <figure|div><svg><image xlink:href/></svg></figure|div> with <img>.

        This is the critical fix for Kindle AZW3: Kindle's renderer has poor
        support for <image> elements nested inside <svg>.
        """
        html_dir = os.path.dirname(html_path)

        def _replace(match):
            wrapper_attrs = match.group(2) or ''
            svg_content = match.group(3)

            img_m = re.search(r'<image\b([^>]*?)/?>', svg_content)
            if not img_m:
                return match.group(0)

            img_attrs = img_m.group(1)
            href_m = re.search(r'xlink:href="([^"]+)"', img_attrs)
            if not href_m:
                return match.group(0)

            abs_path = self._resolve_path(html_dir, href_m.group(1))
            if abs_path in self.image_rename_map:
                new_img_path = self._get_new_image_path(abs_path)
                new_src = self._make_relative(html_dir, new_img_path)
            else:
                new_src = href_m.group(1)

            w_m = re.search(r'width="([^"]+)"', img_attrs)
            h_m = re.search(r'height="([^"]+)"', img_attrs)
            dims = ''
            if w_m:
                dims += f' width="{w_m.group(1)}"'
            if h_m:
                dims += f' height="{h_m.group(1)}"'
            img_tag = f'<img alt="" src="{new_src}"{dims} class="calibre1"/>'

            class_m = re.search(r'class="([^"]*)"', wrapper_attrs)
            cls = class_m.group(1) if class_m else ''
            result = f'<div class="{cls}">{img_tag}</div>' if cls else f'<div>{img_tag}</div>'
            return result

        pattern = r'<(figure|div)\b([^>]*)>\s*(<svg\b.*?</svg>)\s*</\1>'
        content = re.sub(pattern, _replace, content, flags=re.DOTALL)
        content = self._update_image_refs(content, html_path)
        return content

    def _update_image_refs(self, content, html_path):
        """Update all image src/xlink:href references to renamed paths."""
        html_dir = os.path.dirname(html_path)

        def _replace_xlink(m):
            abs_path = self._resolve_path(html_dir, m.group(1))
            if abs_path in self.image_rename_map:
                new_path = self._get_new_image_path(abs_path)
                return f'xlink:href="{self._make_relative(html_dir, new_path)}"'
            return m.group(0)

        def _replace_src(m):
            if m.group(1).startswith('http'):
                return m.group(0)
            abs_path = self._resolve_path(html_dir, m.group(1))
            if abs_path in self.image_rename_map:
                new_path = self._get_new_image_path(abs_path)
                return f'src="{self._make_relative(html_dir, new_path)}"'
            return m.group(0)

        content = re.sub(r'xlink:href="([^"]+)"', _replace_xlink, content)
        content = re.sub(r'src="([^"]+)"', _replace_src, content)
        return content

    def _get_new_image_path(self, abs_path):
        if abs_path in self.image_rename_map:
            new_name = self.image_rename_map[abs_path]
            dir_part = os.path.dirname(abs_path)
            if dir_part and dir_part != '.':
                return f"{dir_part}/{new_name}"
            return new_name
        return abs_path

    def _make_relative(self, from_dir, to_path):
        if not from_dir or from_dir == '.':
            return to_path
        from_parts = from_dir.split('/')
        to_parts = to_path.split('/')
        common = 0
        for a, b in zip(from_parts, to_parts):
            if a == b:
                common += 1
            else:
                break
        up = len(from_parts) - common
        return '/'.join(['..'] * up + list(to_parts[common:]))

    def _decode_text(self, data):
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            for enc in ['shift-jis', 'gbk', 'gb2312', 'big5', 'latin-1']:
                try:
                    return data.decode(enc)
                except Exception:
                    pass
            return data.decode('utf-8', errors='replace')

    # ------------------------------------------------------------------
    # OPF fixing
    # ------------------------------------------------------------------

    def _fix_root_opf(self):
        content = self.root_opf_xml
        # Normalize non-standard media types from epubmerge
        content = content.replace(
            'media-type="origrootfile/xml"',
            'media-type="application/oebps-package+xml"')
        content = content.replace(
            'media-type="origtocncx/xml"',
            'media-type="application/x-dtbncx+xml"')

        opf_dir = os.path.dirname(self.root_opf_path)

        def _update_manifest_item(match):
            tag = match.group(0)
            href_m = re.search(r'href="([^"]+)"', tag)
            if not href_m:
                return tag
            orig_href = href_m.group(1)
            abs_path = self._resolve_path(opf_dir, orig_href)
            if abs_path not in self.image_rename_map:
                return tag

            new_name = self.image_rename_map[abs_path]
            dir_part = os.path.dirname(orig_href)
            new_href = f"{dir_part}/{new_name}" if dir_part else new_name
            tag = tag.replace(f'href="{orig_href}"', f'href="{new_href}"')

            # Update item id to match new filename
            old_id_m = re.search(r'id="([^"]+)"', tag)
            if old_id_m:
                old_id = old_id_m.group(1)
                new_base = os.path.splitext(new_name)[0]
                prefix_m = re.match(r'(a\d+img).*', old_id)
                new_id = f'{prefix_m.group(1)}{new_base}' if prefix_m else f'img{new_base}'
                tag = tag.replace(f'id="{old_id}"', f'id="{new_id}"')
            return tag

        content = re.sub(
            r'<item\b[^>]*?media-type="image/[^"]*"[^>]*/?>',
            _update_manifest_item, content)

        self.root_opf_xml = content
        self.zip_entries[self.root_opf_path] = content.encode('utf-8')

    def _fix_sub_opfs(self):
        for path in self.file_list:
            if not path.lower().endswith('.opf'):
                continue
            if path == self.root_opf_path:
                continue
            content = self._decode_text(self.zip_entries[path])
            content = content.replace(
                'media-type="origrootfile/xml"',
                'media-type="application/oebps-package+xml"')
            content = content.replace(
                'media-type="origtocncx/xml"',
                'media-type="application/x-dtbncx+xml"')

            opf_dir = os.path.dirname(path)

            def _update_sub_manifest(match):
                tag = match.group(0)
                href_m = re.search(r'href="([^"]+)"', tag)
                if not href_m:
                    return tag
                orig_href = href_m.group(1)
                abs_path = self._resolve_path(opf_dir, orig_href)
                if abs_path not in self.image_rename_map:
                    return tag
                new_name = self.image_rename_map[abs_path]
                dir_part = os.path.dirname(orig_href)
                new_href = f"{dir_part}/{new_name}" if dir_part else new_name
                tag = tag.replace(f'href="{orig_href}"', f'href="{new_href}"')

                old_id_m = re.search(r'id="([^"]+)"', tag)
                if old_id_m:
                    old_id = old_id_m.group(1)
                    new_base = os.path.splitext(new_name)[0]
                    prefix_m = re.match(r'(img).*', old_id)
                    new_id = f'img{new_base}' if prefix_m else f'img{new_base}'
                    tag = tag.replace(f'id="{old_id}"', f'id="{new_id}"')
                return tag

            content = re.sub(
                r'<item\b[^>]*?media-type="image/[^"]*"[^>]*/?>',
                _update_sub_manifest, content)
            self.zip_entries[path] = content.encode('utf-8')

    # ------------------------------------------------------------------
    # Image renaming (in-place within each volume directory)
    # ------------------------------------------------------------------

    def _rename_images(self):
        new_entries = {}
        removed_paths = set()

        for old_path, new_name in self.image_rename_map.items():
            if old_path not in self.zip_entries:
                continue
            dir_part = os.path.dirname(old_path)
            new_path = f"{dir_part}/{new_name}" if dir_part else new_name
            if old_path != new_path:
                new_entries[new_path] = self.zip_entries[old_path]
                removed_paths.add(old_path)

        for old_path in removed_paths:
            del self.zip_entries[old_path]
        self.zip_entries.update(new_entries)

        # Update file_list: remove old paths, add new paths (avoiding duplicates)
        self.file_list = [f for f in self.file_list if f not in removed_paths]
        current_set = set(self.file_list)
        for new_path in new_entries:
            if new_path not in current_set:
                self.file_list.append(new_path)
                current_set.add(new_path)
