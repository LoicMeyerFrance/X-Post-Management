"""Tests for the documents the user hands to the assistant.

The agent has no file access. These tools serve exactly one folder the user
chose, so the only thing that really matters here is that nothing resolves
outside it - not through `..`, not through an absolute path, not through a
symlink planted inside the folder.

    python tests/test_sources.py
"""

import os
import shutil
import sys
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix='xpm-sources-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import app as appmod        # noqa: E402
import config               # noqa: E402
import sources              # noqa: E402

config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'

LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


def build_workspace():
    """A folder to share, and a secret next to it that must stay unreachable."""
    base = tempfile.mkdtemp(prefix='xpm-docs-')
    shared = os.path.join(base, 'shared')
    os.makedirs(os.path.join(shared, 'notes'), exist_ok=True)

    with open(os.path.join(shared, 'brief.md'), 'w', encoding='utf-8') as handle:
        handle.write('# Launch brief\n\nThree points worth posting about.\n')
    with open(os.path.join(shared, 'notes', 'raw.txt'), 'w', encoding='utf-8') as handle:
        handle.write('rough notes\n')
    with open(os.path.join(shared, 'photo.png'), 'wb') as handle:
        handle.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
    with open(os.path.join(shared, 'report.pdf'), 'wb') as handle:
        handle.write(b'%PDF-1.4\n' + b'x' * 64)
    # A text extension over a binary body: the name must not be believed.
    with open(os.path.join(shared, 'fake.txt'), 'wb') as handle:
        handle.write(b'text\x00\x00binary')

    with open(os.path.join(base, 'secret.md'), 'w', encoding='utf-8') as handle:
        handle.write('PRIVATE-MARKER-4417\n')
    return base, shared


def test_choosing():
    section('choosing what to share')

    base, shared = build_workspace()
    try:
        sources.set_root('')
        check('nothing is shared to begin with', sources.get_root() == '')
        try:
            sources.list_files()
            check('listing without a choice is refused', False, 'no error raised')
        except sources.SourceError as exc:
            check('listing without a choice is refused', 'not given you' in str(exc), str(exc))

        result = sources.set_root(shared)
        check('a folder can be chosen', result['kind'] == 'folder', result)
        check('and is remembered',
              os.path.realpath(sources.get_root()) == os.path.realpath(shared))

        info = sources.describe()
        check('describe reports it exists', info['exists'] is True, info)
        check('and counts the readable files', info['count'] >= 2, info)

        single = os.path.join(shared, 'brief.md')
        check('a single file can be chosen', sources.set_root(single)['kind'] == 'file')

        try:
            sources.set_root(os.path.join(base, 'does-not-exist'))
            check('a missing path is refused', False, 'no error raised')
        except sources.SourceError as exc:
            check('a missing path is refused', 'No such' in str(exc), str(exc))

        sources.set_root('')
        check('access can be taken away again', sources.get_root() == '')
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_listing_and_reading():
    section('listing and reading')

    base, shared = build_workspace()
    try:
        sources.set_root(shared)
        listing = sources.list_files()
        names = sorted(f['name'] for f in listing['files'])
        check('the text files are listed', 'brief.md' in names, names)
        check('nested files are listed too',
              any(n.endswith('raw.txt') for n in names), names)
        check('an image is not listed', not any(n.endswith('.png') for n in names), names)
        check('a binary pretending to be text is still listed by name',
              'fake.txt' in names, names)

        # A PDF is named rather than dropped, so the agent can say why.
        skipped = [s['name'] for s in listing.get('skipped', [])]
        check('a PDF is reported as unreadable', 'report.pdf' in skipped, skipped)

        content = sources.read_file('brief.md')
        check('a document can be read', 'Launch brief' in content['text'], content)
        check('and reports its size', content['bytes'] > 0, content)
        check('and is not truncated when short', content['truncated'] is False)

        check('a nested document can be read',
              'rough notes' in sources.read_file(os.path.join('notes', 'raw.txt'))['text'])

        for name, expected in (('photo.png', 'not a text document'),
                               ('report.pdf', 'cannot read'),
                               ('fake.txt', 'does not contain text')):
            try:
                sources.read_file(name)
                check(f'{name} is refused', False, 'no error raised')
            except sources.SourceError as exc:
                check(f'{name} is refused', expected in str(exc), str(exc))

        try:
            sources.read_file('nope.md')
            check('a missing document is refused', False, 'no error raised')
        except sources.SourceError as exc:
            check('a missing document is refused', 'No such document' in str(exc), str(exc))

        # A single chosen file is the only thing readable.
        sources.set_root(os.path.join(shared, 'brief.md'))
        check('a single file reads without a name',
              'Launch brief' in sources.read_file()['text'])
        try:
            sources.read_file('raw.txt')
            check('and nothing else is reachable', False, 'no error raised')
        except sources.SourceError as exc:
            check('and nothing else is reachable', 'only document' in str(exc), str(exc))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_nothing_escapes():
    """The one thing that must not be got wrong."""
    section('nothing escapes the chosen folder')

    base, shared = build_workspace()
    try:
        sources.set_root(shared)
        secret = os.path.join(base, 'secret.md')

        escapes = [
            os.path.join('..', 'secret.md'),
            os.path.join('..', '..', 'secret.md'),
            'notes/../../secret.md',
            secret,                                   # an absolute path
            os.path.join('notes', '..', '..', 'secret.md'),
        ]
        for attempt in escapes:
            try:
                got = sources.read_file(attempt)
                check(f'{attempt!r} is blocked', False,
                      'READ ' + got['text'][:40])
            except sources.SourceError as exc:
                check(f'{attempt!r} is blocked',
                      'outside' in str(exc) or 'No such' in str(exc), str(exc))

        # A symlink planted inside the folder points out of it. Resolving before
        # checking is what catches this; comparing the unresolved path would not.
        link = os.path.join(shared, 'shortcut.md')
        made = False
        try:
            os.symlink(secret, link)
            made = True
        except (OSError, NotImplementedError, AttributeError):
            check('symlink escape blocked (skipped: cannot create links here)', True)
        if made:
            try:
                got = sources.read_file('shortcut.md')
                check('a symlink out of the folder is blocked', False,
                      'READ ' + got['text'][:40])
            except sources.SourceError as exc:
                check('a symlink out of the folder is blocked', 'outside' in str(exc),
                      str(exc))

        # And the marker never appears in a listing either.
        listing = sources.list_files()
        blob = str(listing)
        check('the private file is not listed', 'secret.md' not in blob, blob[:200])

        # A sibling folder whose name merely starts the same must not be reachable.
        sibling = shared + '-private'
        os.makedirs(sibling, exist_ok=True)
        with open(os.path.join(sibling, 'other.md'), 'w', encoding='utf-8') as handle:
            handle.write('PRIVATE-MARKER-4417\n')
        try:
            sources.read_file(os.path.join('..', os.path.basename(sibling), 'other.md'))
            check('a same-prefix sibling folder is blocked', False, 'no error raised')
        except sources.SourceError as exc:
            check('a same-prefix sibling folder is blocked', 'outside' in str(exc), str(exc))
    finally:
        sources.set_root('')
        shutil.rmtree(base, ignore_errors=True)


def test_limits():
    section('limits')

    base, shared = build_workspace()
    try:
        big = os.path.join(shared, 'huge.txt')
        with open(big, 'w', encoding='utf-8') as handle:
            handle.write('x' * (sources.MAX_FILE_BYTES + 1024))
        sources.set_root(shared)

        listed = next(f for f in sources.list_files()['files'] if f['name'] == 'huge.txt')
        check('an oversized file is flagged in the listing', listed['too_large'] is True)
        try:
            sources.read_file('huge.txt')
            check('and refused on read', False, 'no error raised')
        except sources.SourceError as exc:
            check('and refused on read', 'over the' in str(exc), str(exc))

        # Long but allowed: truncated, and the result says so.
        long_path = os.path.join(shared, 'long.md')
        with open(long_path, 'w', encoding='utf-8') as handle:
            handle.write('y' * (sources.MAX_EXCERPT + 500))
        result = sources.read_file('long.md')
        check('a long document is truncated', result['truncated'] is True)
        check('to the documented limit', len(result['text']) == sources.MAX_EXCERPT,
              len(result['text']))
    finally:
        sources.set_root('')
        shutil.rmtree(base, ignore_errors=True)


def test_file_dialog_filter():
    """The document picker's filter must be one pywebview will accept.

    It validates the filter and raises otherwise, which the app logged and turned
    into an empty answer - so the button opened nothing and said nothing. Spaces
    between extensions were the cause; it wants semicolons.
    """
    section("the document picker's filter")

    readable = ';'.join('*' + extension for extension in sorted(sources.TEXT_EXTENSIONS))
    built = f'Text documents ({readable})'

    try:
        from webview.util import parse_file_type
    except Exception:
        check('pywebview available to validate the filter (skipped)', True)
        return

    try:
        parse_file_type(built)
        check('pywebview accepts the document filter', True)
    except ValueError as exc:
        check('pywebview accepts the document filter', False, str(exc))

    try:
        parse_file_type('All Files (*.*)')
        check('and the catch-all filter', True)
    except ValueError as exc:
        check('and the catch-all filter', False, str(exc))

    # The shape that was actually broken, so the fix cannot be undone quietly.
    spaced = 'Text documents (' + ' '.join('*' + e for e in sorted(sources.TEXT_EXTENSIONS)) + ')'
    try:
        parse_file_type(spaced)
        check('space-separated extensions would have been accepted', False,
              'pywebview no longer rejects them; the test has lost its point')
    except ValueError:
        check('space-separated extensions are rejected, as they were', True)

    check('every readable format is offered',
          all(('*' + extension) in built for extension in sources.TEXT_EXTENSIONS))


def test_api(client):
    section('the API')

    base, shared = build_workspace()
    try:
        r = client.post('/api/agent/sources', json={'path': shared}, headers=LOCAL)
        check('a folder can be set over HTTP', r.status_code == 200, r.status_code)
        check('and reported back', r.get_json().get('kind') == 'folder', r.get_json())

        r = client.get('/api/agent/sources', headers=LOCAL)
        check('the choice reads back', r.get_json().get('exists') is True, r.get_json())

        r = client.get('/api/agent/sources/list', headers=LOCAL)
        names = [f['name'] for f in r.get_json().get('files', [])]
        check('the listing comes back', 'brief.md' in names, names)

        r = client.get('/api/agent/sources/read?name=brief.md', headers=LOCAL)
        check('a document can be read over HTTP',
              'Launch brief' in r.get_json().get('text', ''), r.get_json())

        # The escape attempt must fail through the API too, not only in-process.
        r = client.get('/api/agent/sources/read?name=../secret.md', headers=LOCAL)
        body = r.get_json()
        check('an escape is refused over HTTP', 'error' in body, body)
        check('and leaks nothing', 'PRIVATE-MARKER-4417' not in str(body), str(body)[:120])

        r = client.post('/api/agent/sources', json={'path': ''}, headers=LOCAL)
        check('access can be cleared over HTTP', r.get_json().get('root') == '')

        r = client.post('/api/agent/sources', json={'path': os.path.join(base, 'nope')},
                        headers=LOCAL)
        check('a missing path is a 400', r.status_code == 400, r.status_code)

        # The guards apply here as everywhere else.
        r = client.get('/api/agent/sources', headers={'Host': 'evil.com'})
        check('foreign Host blocked', r.status_code == 403, r.status_code)
        r = client.post('/api/agent/sources', json={'path': shared},
                        headers={'Host': '127.0.0.1:5000', 'Origin': 'https://evil.com'})
        check('cross-origin cannot point it somewhere', r.status_code == 403, r.status_code)
    finally:
        sources.set_root('')
        shutil.rmtree(base, ignore_errors=True)


def main():
    print('=' * 62)
    print('  Document access tests')
    print('=' * 62)

    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()

    test_choosing()
    test_listing_and_reading()
    test_nothing_escapes()
    test_limits()
    test_file_dialog_filter()
    test_api(client)

    print('\n' + '=' * 62)
    print(f'  {len(passed)} passed, {len(failed)} failed')
    if failed:
        print('\n  Failures:')
        for name in failed:
            print(f'    - {name}')
    print('=' * 62)
    return 1 if failed else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        config.delete_password()
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
