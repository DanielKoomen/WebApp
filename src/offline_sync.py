import logging
from sqlite3 import Connection
import traceback
from urllib.parse import quote as urlencode

import requests
from requests import Response
from requests.exceptions import RequestException

import settings
import db
import logconfig


log = logging.getLogger('app.offline')


class OfflineSync:
    db_offline: Connection
    db_music: Connection
    base_url: str
    token: str | None = None

    def __init__(self, db_offline: Connection, db_music: Connection):
        self.db_offline = db_offline
        self.db_music = db_music
        self.set_base_url()
        self.set_token()

    def set_base_url(self):
        row = self.db_offline.execute("SELECT value FROM settings WHERE key='sync_url'").fetchone()
        if row:
            self.base_url, = row
        else:
            log.info('No sync server is configured')
            self.base_url = input('Enter server URL (https://example.com): ')
            self.db_offline.execute("INSERT INTO settings (key, value) VALUES ('sync_url', ?)",
                                    (self.base_url,))
            self.db_offline.commit()

    def request_get(self, route: str) -> Response:
        response = requests.get(self.base_url + route,
                                headers={'Cookie': 'token=' + self.token,
                                         'User-Agent': settings.user_agent},
                                timeout=30)
        return response

    def request_post(self, route: str, data) -> Response:
        headers = {'User-Agent': settings.user_agent}
        if self.token is not None:
            headers['Cookie'] = 'token=' + self.token
        response = requests.post(self.base_url + route,
                                 json=data,
                                 headers=headers,
                                 timeout=30)
        return response

    def set_token(self):
        row = self.db_offline.execute("SELECT value FROM settings WHERE key='sync_token'").fetchone()
        if row:
            self.token, = row
            try:
                self.request_get('/get_csrf')
                log.info('Authentication token is valid')
            except RequestException:
                traceback.print_exc()
                log.info('Error testing authentication token. Please log in again.')
                self.login_prompt()
                self.set_token()
        else:
            log.info('No authentication token stored, please log in')
            self.login_prompt()
            self.set_token()

    def login_prompt(self):
        username = input('Enter username: ')
        password = input('Enter password: ')

        try:
            response = self.request_post('/login',
                                         {'username': username,
                                          'password': password})
        except RequestException:
            traceback.print_exc()
            log.info('Error during log in, please try again.')
            self.login_prompt()
            return

        token = response.json()['token']
        self.db_offline.execute("INSERT INTO settings (key, value) VALUES ('sync_token', ?)",
                                (token,))
        self.db_offline.commit()
        log.info('Logged in successfully')

    def _download_track_content(self, path: str):
        log.info('Downloading audio data')
        response = self.request_get('/get_track?type=webm_opus_high&path=' + urlencode(path))
        assert response.status_code == 200
        music_data = response.content

        log.info('Downloading album cover')
        response = self.request_get('/get_album_cover?quality=high&path=' + urlencode(path))
        assert response.status_code == 200
        cover_data = response.content
        log.info('Downloading lyrics')
        response = self.request_get('/get_lyrics?path=' + urlencode(path))
        assert response.status_code == 200
        lyrics_json = response.text

        self.db_offline.execute(
            """
            INSERT INTO content (path, music_data, cover_data, lyrics_json)
            VALUES(:path, :music_data, :cover_data, :lyrics_json)
            ON CONFLICT (path) DO UPDATE SET
                music_data = :music_data, cover_data = :cover_data, lyrics_json = :lyrics_json
            """,
            {'path': path,
             'music_data': music_data,
             'cover_data': cover_data,
             'lyrics_json': lyrics_json})

    def _update_track(self, track):
        self._download_track_content(track['path'])

        log.info('Updating metadata')

        self.db_music.execute('UPDATE track SET duration=?, title=?, album=?, album_artist=?, year=?, mtime=? WHERE path=?',
                              (track['duration'], track['title'], track['album'], track['album_artist'], track['year'],
                               track['mtime'], track['path']))

        self.db_music.execute('DELETE FROM track_artist WHERE track=?', (track['path'],))

        if track['artists']:
            insert = [(track['path'], artist) for artist in track['artists']]
            self.db_music.executemany('INSERT INTO track_artist (track, artist) VALUES (?, ?)', insert)

    def _insert_track(self, playlist, track):
        self._download_track_content(track['path'])

        log.info('Storing metadata')

        self.db_music.execute(
            """
            INSERT INTO track (path, playlist, duration, title, album,
                               album_artist, year, mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (track['path'], playlist['name'], track['duration'], track['title'],
             track['album'], track['album_artist'], track['year'], track['mtime'])
        )

        if track['artists']:
            insert = [(track['path'], artist) for artist in track['artists']]
            self.db_music.executemany('INSERT INTO track_artist (track, artist) VALUES (?, ?)', insert)

    def _prune_tracks(self, track_paths: set[str]):
        rows = self.db_music.execute('SELECT path FROM track').fetchall()
        for path, in rows:
            if path not in track_paths:
                log.info('Delete: %s', path)
                self.db_offline.execute('DELETE FROM content WHERE path=?',
                                        (path,))
                self.db_music.execute('DELETE FROM track WHERE path=?',
                                      (path,))

    def _prune_playlists(self, playlists: set[str]):
        rows = self.db_music.execute('SELECT path FROM playlist').fetchall()
        for name, in rows:
            if name not in playlists:
                log.info('Delete playlist: %s', name)
                self.db_music.execute('DELETE FROM playlist WHERE path=?',
                                      (name,))

    def sync_tracks(self):
        log.info('Downloading track list')
        response = self.request_get('/track_list').json()

        track_paths: set[str] = set()

        for playlist in response['playlists']:
            if not playlist['favorite']:
                continue

            self.db_music.execute('INSERT INTO playlist VALUES (?) ON CONFLICT (path) DO NOTHING',
                                  (playlist['name'],))

            for track in playlist['tracks']:
                track_paths.add(track['path'])

                row = self.db_music.execute('SELECT mtime FROM track WHERE path=?',
                                            (track['path'],)).fetchone()
                if row:
                    mtime, = row
                    if mtime == track['mtime']:
                        # log.info('Up to date: %s', track['path'])
                        pass
                    else:
                        log.info('Out of date: %s', track['path'])
                        self._update_track(track)
                else:
                    log.info('Missing: %s', track['path'])
                    self._insert_track(playlist, track)

                self.db_offline.commit()
                self.db_music.commit()

        self._prune_tracks(track_paths)
        self._prune_playlists({playlist['name'] for playlist in response['playlists']})

    def sync_history(self):
        csrf_token = self.request_get('/get_csrf').json()['token']

        rows = self.db_offline.execute('SELECT rowid, timestamp, track, playlist FROM history ORDER BY timestamp ASC')
        for rowid, timestamp, track, playlist in rows:
            log.info('Played: %s', track)
            duration_row = self.db_music.execute('SELECT duration FROM track WHERE path=?', (track,)).fetchone()
            if duration_row:
                duration, = duration_row
                lastfm = duration > 30
            else:
                log.warning('Duration unknown, assuming not eligible for scrobbling')
                lastfm = False
            response = self.request_post('/history_played',
                              {'csrf': csrf_token,
                               'track': track,
                               'playlist': playlist,
                               'timestamp': timestamp,
                               'lastfmEligible': lastfm})
            assert response.status_code == 200
            self.db_offline.execute('DELETE FROM history WHERE rowid=?', (rowid,))
            self.db_offline.commit()


def main():
    if not settings.offline_mode:
        return

    with db.connect() as db_music, db.offline() as db_offline:
        sync = OfflineSync(db_offline, db_music)
        log.info('Sync history')
        sync.sync_history()
        log.info('Sync tracks')
        sync.sync_tracks()
        log.info('Done! Please wait for program to exit.')


if __name__ == '__main__':
    logconfig.apply()
    main()
