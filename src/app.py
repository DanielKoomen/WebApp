from typing import Optional
import hashlib
import hmac
import logging
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, request, render_template, Response, redirect
import flask_assets
from flask_babel import Babel

from assets import Assets
import bing
import genius
import image
import settings
import music
import logconfig
from metadata import Metadata
from music import Track
import musicbrainz


application = Flask(__name__, template_folder=Path('templates'))
babel = Babel(application)
flask_assets.Environment(application)
assets = Assets()
log = logging.getLogger('app')
assets_dir = Path('static')
raphson_png_path = Path(assets_dir, 'raphson.png')


def check_password(password: Optional[str]) -> bool:
    """
    Check whether the provided password matches the correct password
    """
    if password is None:
        return False

    # First hash passwords so they have the same length
    # otherwise compare_digest still leaks length

    hashed_pass = hashlib.sha256(password.encode()).digest()
    hashed_correct = hashlib.sha256(settings.web_password.encode()).digest()

    # Constant time comparison
    return hmac.compare_digest(hashed_pass, hashed_correct)


def check_password_cookie() -> bool:
    return check_password(request.cookies.get('password'))


@application.route('/login', methods=['GET', 'POST'])
def login():
    """
    Login route. Serve login page for GET requests, and accepts password input for POST requests.
    If the provided password is invalid, the login template is rendered with invalid_password=True
    """
    if request.method == 'POST':
        if 'password' not in request.form:
            return 'invalid form input'

        password = request.form['password']

        if not check_password(password):
            return render_template('login.jinja2', invalid_password=True)

        response = redirect('/')
        response.set_cookie('password', password, max_age=3600*24*30, samesite='Strict')
        return response
    else:
        return render_template('login.jinja2', invalid_password=False)


@application.route('/')
def player():
    """
    Main player page. Serves player.jinja2 template file.
    """
    if not check_password_cookie():
        return redirect('/login')

    mobile = False
    if 'User-Agent' in request.headers:
        user_agent = request.headers['User-Agent']
        if 'Android' in user_agent or 'iOS' in user_agent:
            mobile = True

    return render_template('player.jinja2',
                           main_playlists=music.playlists(guest=False),
                           guest_playlists=music.playlists(guest=True),
                           assets=assets.all_assets_dict(),
                           mobile=mobile)


@application.route('/choose_track', methods=['GET'])
def choose_track():
    """
    Choose random track from the provided playlist directory.
    """
    if not check_password_cookie():
        return Response(None, 403)

    dir_name = request.args['playlist_dir']
    playlist = music.playlist(dir_name)
    chosen_track = playlist.choose_track()

    return {
        'path': chosen_track.relpath,
    }


@application.route('/get_track')
def get_track() -> Response:
    """
    Get transcoded audio for the given track path.
    """
    if not check_password_cookie():
        return Response(None, 403)

    quality = request.args['quality'] if 'quality' in request.args else 'high'

    track = Track.by_relpath(request.args['path'])
    audio = track.transcoded_audio(quality)
    return Response(audio, mimetype='audio/ogg')


def get_cover_bytes(meta: Metadata) -> Optional[bytes]:
    """
    Find album cover using MusicBrainz or Bing.
    Parameters:
        meta: Track metadata
    Returns: Album cover image bytes, or None if MusicBrainz nor bing found an image.
    """
    log.info('Finding cover for: %s', meta.relpath)

    # Try MusicBrainz first
    mb_query = meta.album_release_query()
    if image_bytes := musicbrainz.get_cover(mb_query):
        return image_bytes

    # Otherwise try bing
    for query in meta.album_search_queries():
        if image_bytes := bing.image_search(query):
            return image_bytes

    log.info('No suitable cover found')
    return None

@application.route('/get_album_cover')
def get_album_cover() -> Response:
    """
    Get album cover image for the provided track path.
    """
    if not check_password_cookie():
        return Response(None, 403)

    track = Track.by_relpath(request.args['path'])

    def get_img():
        meta = track.metadata()
        img = get_cover_bytes(meta)
        return img

    img_format = get_img_format()

    comp_bytes = image.thumbnail(get_img, track.relpath, img_format[6:], 700,
                                 thumb_quality=request.args['quality'])

    return Response(comp_bytes, mimetype=img_format)


@application.route('/get_lyrics')
def get_lyrics():
    """
    Get lyrics for the provided track path.
    """
    if not check_password_cookie():
        return Response(None, 403)

    track = music.Track(request.args['path'])
    meta = track.metadata()

    for search_query in meta.lyrics_search_queries():
        lyrics = genius.get_lyrics(search_query)
        if lyrics is not None:
            return {
                'found': True,
                'source': lyrics.source_url,
                'html': lyrics.lyrics_html(),
            }

    return {'found': False}


@application.route('/ytdl', methods=['POST'])
def ytdl():
    """
    Use yt-dlp to download the provided URL to a playlist directory
    """
    if not check_password_cookie():
        return Response(None, 403)

    directory = request.json['directory']
    url = request.json['url']

    playlist = music.playlist(directory)
    log.info('ytdl %s %s', directory, url)

    result = playlist.download(url)

    return {
        'code': result.returncode,
        'stdout': result.stdout,
        'stderr': result.stderr,
    }


@application.route('/track_list')
def track_list():
    """
    Return list of playlists and tracks. If it takes too long to load metadata for all tracks,
    a partial result is returned.
    """
    if not check_password_cookie():
        return Response(None, 403)

    response = {
        'playlists': {},
        'tracks': [],
        'index': 0,
        'partial': False,
    }

    playlists = music.playlists()

    for playlist in playlists:
        response['playlists'][playlist.relpath] = {
            'dir_name': playlist.relpath,
            'display_name': playlist.name,
            'track_count': playlist.track_count,
            'guest': playlist.guest,
        }

    max_seconds = 5
    skip_to_index = int(request.args['skip']) if 'skip' in request.args else 0
    start_time = datetime.now()

    for playlist in playlists:
        for track in playlist.tracks():
            if skip_to_index <= response['index']:
                meta = track.metadata()
                response['tracks'].append({
                    'path': track.relpath,
                    'display': meta.display_title(),
                    'playlist': playlist.relpath,
                    'playlist_display': playlist.name,
                    'duration': meta.duration,
                    'tags': meta.tags,
                    'title': meta.title,
                    'artists': meta.artists,
                    'album': meta.album,
                })

            if datetime.now() - start_time > timedelta(seconds=max_seconds):
                response['partial'] = True
                return response

            response['index'] += 1

    return response


@application.route('/raphson')
def raphson() -> Response:
    """
    Serve raphson logo image
    """
    if not check_password_cookie():
        return Response(None, 403)

    img_format = get_img_format()
    thumb = image.thumbnail(raphson_png_path, 'raphson', img_format[6:], 512)
    response = Response(thumb, mimetype=img_format)
    response.cache_control.max_age = 24*3600
    return response


@babel.localeselector
def get_locale():
    """
    Get locale preference from HTTP headers
    """
    # TODO language from cookie
    lang = request.accept_languages.best_match(['nl', 'nl-NL', 'nl-BE', 'en'])
    return lang[:2] if lang is not None else 'en'


def get_img_format():
    """
    Get preferred image format
    """
    if 'Accept' in request.headers:
        accept = request.headers['Accept']
        for mime in ['image-avif', 'image/webp']:
            if mime in accept:
                return mime

    # Once webp is working in Accept header for all image requests, this can be changed to jpeg
    # For now assume the browser supports WEBP to avoid always sending JPEG
    return 'image/webp'
