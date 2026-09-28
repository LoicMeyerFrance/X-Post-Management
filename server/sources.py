"""Documents the user has deliberately handed to the assistant.

The agent has no file access: that is the whole point of `--tools ""`. But being
able to say "write three posts from this report" is exactly the job, so instead
of giving it the filesystem, the user names one folder or one file and the app
serves that, and nothing else, through its own MCP tools.

Everything here exists to keep that promise:

  * a resolved path must sit inside the chosen root, checked after resolving
    symlinks, so neither `..` nor a link planted in the folder reaches outside;
  * only text-ish files are served, by extension *and* by sniffing the bytes, so
    a renamed binary cannot come back as mojibake the model then quotes;
  * sizes are capped, because a model reading a 200 MB log helps nobody.
"""

import json
import logging
import os

import paths

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(paths.DATA_DIR, 'sources.json')

# Formats worth reading as text. Deliberately not .pdf or .docx: those need
# parsing, and returning their raw bytes would be worse than refusing.
TEXT_EXTENSIONS = frozenset({
    '.txt', '.md', '.markdown', '.rst', '.csv', '.tsv', '.json', '.yaml', '.yml',
    '.log', '.html', '.htm', '.xml', '.ini', '.cfg', '.toml', '.srt', '.vtt',
})

# Named so the agent can tell the user why a file is missing from the list
# instead of insisting it should be there.
KNOWN_BUT_UNREADABLE = frozenset({'.pdf', '.docx', '.doc', '.odt', '.pptx', '.xlsx'})

MAX_FILE_BYTES = 2 * 1024 * 1024        # one document, not an archive
MAX_LISTED = 200                        # a folder listing stays answerable
MAX_EXCERPT = 200_000                   # characters returned in one read


class SourceError(Exception):
    """Something the agent should be told plainly."""


# --- what the user chose ---------------------------------------------------

def _read_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_root():
    """The folder or file the assistant may read, or '' when none is set."""
    return str(_read_config().get('root') or '')


def set_root(path):
    """Point the assistant at a folder or a file. '' takes the access away."""
    path = str(path or '').strip()
    if not path:
        _write_config({})
        logger.info("Assistant document access cleared")
        return {'root': '', 'kind': ''}

    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.exists(resolved):
        raise SourceError(f'No such folder or file: {path}')

    kind = 'folder' if os.path.isdir(resolved) else 'file'
    _write_config({'root': resolved, 'kind': kind})
    logger.info("Assistant may now read this %s: %s", kind, resolved)
    return {'root': resolved, 'kind': kind}


def describe():
    config = _read_config()
    root = str(config.get('root') or '')
    info = {'root': root, 'kind': config.get('kind', ''), 'exists': False, 'count': 0}
    if not root:
        return info
    info['exists'] = os.path.exists(root)
    if info['exists'] and os.path.isdir(root):
        try:
            info['count'] = len(list_files()['files'])
        except SourceError:
            info['count'] = 0
    elif info['exists']:
        info['count'] = 1
    return info


# --- serving it safely -----------------------------------------------------

def _root_or_fail():
    root = get_root()
    if not root:
        raise SourceError('The user has not given you any document to read. '
                          'Ask them to choose a folder or a file in the '
                          'Assistant tab, under Documents.')
    if not os.path.exists(root):
        raise SourceError(f'The chosen location no longer exists: {root}')
    return root


def resolve(name):
    """The real path of `name` inside the chosen root.

    Resolved before it is checked: a relative `..`, an absolute path, or a
    symlink planted inside the folder would all otherwise walk straight out of
    it. `commonpath` rather than `startswith`, because "/data/docs-secret"
    starts with "/data/docs".
    """
    root = _root_or_fail()
    if os.path.isfile(root):
        # A single file was chosen: that is the only thing there is to read.
        if name and os.path.basename(name) != os.path.basename(root):
            raise SourceError(f'The only document available is '
                              f'{os.path.basename(root)}')
        return root

    candidate = os.path.realpath(os.path.join(root, name or ''))
    real_root = os.path.realpath(root)
    try:
        if os.path.commonpath([candidate, real_root]) != real_root:
            raise ValueError
    except ValueError:
        raise SourceError(f'{name!r} is outside the folder you were given')
    if not os.path.isfile(candidate):
        raise SourceError(f'No such document: {name}')
    return candidate


def _looks_like_text(path):
    """Read the head and refuse anything with NUL bytes in it."""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return b'\x00' not in head


def list_files():
    """Every readable document under the chosen location."""
    root = _root_or_fail()
    if os.path.isfile(root):
        return {'root': root, 'kind': 'file', 'files': [_describe_file(root, root)]}

    found, skipped, truncated = [], [], False
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in sorted(names):
            full = os.path.join(folder, name)
            extension = os.path.splitext(name)[1].lower()
            if extension in KNOWN_BUT_UNREADABLE:
                skipped.append({'name': os.path.relpath(full, root),
                                'reason': f'{extension} is not readable as text'})
                continue
            if extension not in TEXT_EXTENSIONS:
                continue
            found.append(_describe_file(full, root))
            if len(found) >= MAX_LISTED:
                truncated = True
                break
        if truncated:
            break

    return {'root': root, 'kind': 'folder', 'files': found,
            'skipped': skipped[:20], 'truncated': truncated}


def _describe_file(full, root):
    try:
        size = os.path.getsize(full)
    except OSError:
        size = 0
    return {
        'name': os.path.basename(full) if full == root else os.path.relpath(full, root),
        'bytes': size,
        'too_large': size > MAX_FILE_BYTES,
    }


def read_file(name=''):
    """The text of one document inside the chosen location."""
    path = resolve(name)

    extension = os.path.splitext(path)[1].lower()
    if extension in KNOWN_BUT_UNREADABLE:
        raise SourceError(f'{os.path.basename(path)} is a {extension[1:]} file, which '
                          'this app cannot read as text. Ask the user to export it '
                          'to .txt or .md.')
    if extension not in TEXT_EXTENSIONS:
        raise SourceError(f'{os.path.basename(path)} is not a text document. '
                          f'Readable formats: {", ".join(sorted(TEXT_EXTENSIONS))}')

    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise SourceError(f'{os.path.basename(path)} is {size // 1024 // 1024} MB, over '
                          f'the {MAX_FILE_BYTES // 1024 // 1024} MB limit.')
    if not _looks_like_text(path):
        raise SourceError(f'{os.path.basename(path)} does not contain text, whatever '
                          'its name suggests.')

    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        content = handle.read(MAX_EXCERPT + 1)

    truncated = len(content) > MAX_EXCERPT
    return {
        'name': os.path.basename(path),
        'bytes': size,
        'truncated': truncated,
        'text': content[:MAX_EXCERPT],
    }
