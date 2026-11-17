import json
import pytest
from engine import media_resolver as media


def tweet(identifier='101', owner='42', kind='photo', **extra):
    image = {'id_str': '900', 'type': kind, 'media_url_https': 'https://pbs.twimg.com/media/fixture.jpg',
             'original_info': {'width': 2048, 'height': 3072}}
    image.update(extra)
    return {'rest_id': identifier, 'core': {'user_results': {'result': {'rest_id': owner}}},
            'legacy': {'id_str': identifier, 'user_id_str': owner, 'extended_entities': {'media': [image]}}}


def slot(value):
    return {'entryId': 'tweet-' + value['rest_id'], 'content': {'itemContent': {'tweet_results': {'result': value}}}}


def page(entries=None, cursor='', instructions=None):
    entries = list(entries or [])
    if cursor: entries.append({'entryId': 'cursor-bottom-0', 'content': {'cursorType': 'Bottom', 'value': cursor}})
    return {'data': {'user': {'result': {'rest_id': '42', 'timeline': {'timeline': {'instructions': instructions if instructions is not None else [
        {'type': 'TimelineAddEntries', 'entries': entries}]}}}}}}


@pytest.mark.parametrize('url,handle', [('https://x.com/hazzi_808', 'hazzi_808'),
    ('https://twitter.com/hazzi_808/media?x=1', 'hazzi_808'), ('https://mobile.x.com/HAZZI_808/', 'HAZZI_808'),
    ('https://x.com/hazzi_808/status/123', ''), ('https://x.com/home', ''), ('https://x.com/i/flow/login', ''),
    ('https://x.com.evil.test/hazzi_808', ''), ('https://x.com@evil.test/hazzi_808', ''),
    ('https://x.com:bad/hazzi_808', ''), ('https://x.com/too_long_username_123', '')])
def test_profile_links_are_host_and_path_safe(url, handle):
    assert media.x_profile_handle(url) == handle


def test_page_uses_only_own_photos_not_video_covers_quotes_reposts_or_avatar():
    own = tweet()
    own['quoted_status_result'] = {'result': tweet('999', '999')}
    retweet = tweet('102'); retweet['legacy']['retweeted_status_id_str'] = '900'
    data = page([slot(own), slot(tweet('103', kind='video')), slot(tweet('104', owner='900')),
                 slot(retweet), slot(tweet('105', source_status_id_str='999')),
                 slot(tweet('106', media_url_https='https://pbs.twimg.com/profile_images/avatar.jpg'))], 'next')
    result = media.x_profile_page(data, 'hazzi_808', '42')
    assert [r['id'] for r in result['resources']] == ['101:900']
    assert result['resources'][0]['url'] == 'https://pbs.twimg.com/media/fixture?format=jpg&name=orig'
    assert not result['profileComplete'] and result['profileCursor'] == 'next'


def test_media_grid_and_visibility_wrapper_keep_picture_order_and_deduplicate():
    first = slot(tweet())['content']
    second = slot(tweet('102', media_url_https='https://pbs.twimg.com/media/two?format=png&name=small'))['content']
    data = page([{'content': {'items': [{'item': first}, {'item': second}, {'item': first}]}}])
    result = media.x_profile_page(data, 'hazzi_808', '42')
    assert [r['id'] for r in result['resources']] == ['101:900', '102:900']
    assert result['resources'][1]['url'].endswith('format=png&name=orig')
    assert result['profileComplete']


def test_empty_photo_page_with_continuation_is_not_the_end():
    result = media.x_profile_page(page([slot(tweet(kind='video'))], 'next'), 'hazzi_808', '42')
    assert result['resources'] == [] and not result['profileComplete']
    assert media.x_profile_page(page(), 'hazzi_808', '42')['profileComplete']


@pytest.mark.parametrize('data', [{}, page(instructions=[]), page(instructions=[{'type': 'Unknown'}]),
    page(instructions=[{'type': 'TimelinePinEntry', 'entry': slot(tweet())}]),
    page(instructions=[{'type': 'TimelineAddEntries'}])])
def test_missing_or_unrecognized_page_is_never_reported_complete(data):
    with pytest.raises(media.MediaError): media.x_profile_page(data, 'hazzi_808', '42')


def test_mismatches_and_repeating_cursor_stop_instead_of_fetching_other_content():
    with pytest.raises(media.MediaError, match='MEDIA_ID_MISMATCH'):
        media.x_profile_page(page(), 'hazzi_808', '99')
    with pytest.raises(media.MediaError, match='MEDIA_UNCONFIRMED'):
        media.x_profile_page(page(cursor='same'), 'hazzi_808', '42', 'same')
    value = tweet(); value['legacy']['id_str'] = '99'
    with pytest.raises(media.MediaError, match='MEDIA_ID_MISMATCH'):
        media.x_profile_page(page([slot(value)]), 'hazzi_808', '42')


def test_pinned_adapter_makes_one_photo_page_with_scoped_options_and_checks_user(monkeypatch):
    from gallery_dl import config
    from gallery_dl.extractor.twitter import TwitterAPI
    seen = []
    monkeypatch.setattr(TwitterAPI, 'user_by_screen_name', lambda self, handle: {'rest_id': '42', 'core': {'screen_name': handle}})
    def call(self, endpoint, params):
        seen.append((endpoint, json.loads(params['variables'])))
        return page([slot(tweet())], 'next')
    monkeypatch.setattr(TwitterAPI, '_call', call)
    result = media.resolve_x_profile_page('https://x.com/hazzi_808', proxy='socks5h://127.0.0.1:1080', cursor='previous', expected_user='42')
    assert len(seen) == 1 and seen[0][0].endswith('/UserPhotoTimeline')
    assert seen[0][1]['cursor'] == 'previous' and seen[0][1]['userId'] == '42'
    assert not result['profileComplete']
    assert config.get(('extractor', 'twitter'), 'videos') is False
    assert config.get(('extractor', 'twitter'), 'cookies-update') is False
    assert config.get(('cache',), 'file') == ':memory:'
    with pytest.raises(media.MediaError, match='MEDIA_ID_MISMATCH'):
        media.resolve_x_profile_page('https://x.com/hazzi_808', expected_user='99')
    assert len(seen) == 1


@pytest.mark.parametrize('domain', ['x.com', '.x.com', 'twitter.com'])
def test_host_only_and_legacy_cookie_imports_authenticate_only_x_without_modifying_files(tmp_path, monkeypatch, domain):
    from gallery_dl.extractor.twitter import TwitterAPI
    path = tmp_path / 'scoped.txt'
    path.write_text('# Netscape HTTP Cookie File\n' + f'{domain}\t{"TRUE" if domain.startswith(".") else "FALSE"}\t/\tTRUE\t0\tauth_token\tfixture-session\n'
                    + 'evil.test\tFALSE\t/\tTRUE\t0\tct0\twrong-domain\n', encoding='utf-8')
    original = path.read_bytes()
    def user(self, handle):
        assert self.headers['x-twitter-auth-type'] == 'OAuth2Session'
        assert self.extractor.cookies.get('auth_token',domain='.x.com') == 'fixture-session'
        assert self.headers['x-csrf-token'] != 'wrong-domain'
        return {'rest_id':'42','core':{'screen_name':handle}}
    monkeypatch.setattr(TwitterAPI, 'user_by_screen_name', user)
    monkeypatch.setattr(TwitterAPI, '_call', lambda *args: page())
    assert media.resolve_x_profile_page('https://x.com/hazzi_808',str(path))['profileComplete']
    assert path.read_bytes() == original
