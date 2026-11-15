from datetime import datetime, timezone, timedelta
import httpx
import jwt
from .base import Item, NotConfigured
from ..config import settings
from ..provider_tokens import load_token

API='https://api.music.apple.com/v1'

def developer_configured() -> bool:
    return bool(settings.apple_key_path.is_file() and settings.apple_key_id and settings.apple_team_id)


def developer_token(origin: str | None = None):
    if not developer_configured():
        raise NotConfigured('Apple Music developer credentials missing')
    now=datetime.now(timezone.utc)
    lifetime = timedelta(minutes=15) if origin else timedelta(hours=12)
    claims = {'iss': settings.apple_team_id, 'iat': int(now.timestamp()),
              'exp': int((now + lifetime).timestamp())}
    if origin:
        claims['origin'] = [origin]
    return jwt.encode(claims, settings.apple_key_path.read_text(), algorithm='ES256',
                      headers={'kid': settings.apple_key_id})

def user_token() -> str:
    stored = load_token('apple')
    if stored:
        return stored['user_token']
    if settings.apple_user_token_path.is_file():
        value = settings.apple_user_token_path.read_text().strip()
        if value:
            return value
    raise NotConfigured('Connect Apple Music or provide the Music User Token file')


def headers_for(token: str) -> dict:
    return {'Authorization': f'Bearer {developer_token()}', 'Music-User-Token': token}


async def verify_token(token: str) -> None:
    async with httpx.AsyncClient(timeout=15, headers=headers_for(token)) as client:
        response = await client.get(f'{API}/me/library/songs', params={'limit': 1})
        response.raise_for_status()
        if not isinstance(response.json().get('data'), list):
            raise ValueError('Unexpected response from Apple Music')


async def fetch():
    headers = headers_for(user_token())
    async with httpx.AsyncClient(timeout=35,headers=headers) as client:
        async def paged(path):
            result=[]
            url=f'{API}/{path}'
            while url:
                r=await client.get(url)
                r.raise_for_status()
                d=r.json()
                if not isinstance(d.get('data'),list): raise ValueError('Apple pagination incomplete')
                result.extend(d['data'])
                next_url=d.get('next')
                if next_url and next_url.startswith('/'):
                    url='https://api.music.apple.com'+next_url
                elif next_url and next_url.startswith('https://api.music.apple.com/'):
                    url=next_url
                elif next_url:
                    raise ValueError('Unsafe Apple pagination next URL')
                else: url=None
            return result
        # Apple Library != Apple Favorites. Keep source scopes semantically distinct.
        songs=await paged('me/library/songs')
        playlists=await paged('me/library/playlists')
        out={'library':[],'owned_playlist':[]}
        for s in songs:
            a=s.get('attributes') or {}
            if not s.get('id'):continue
            out['library'].append(Item('library',s['id'],a.get('name') or '',artist=a.get('artistName'),album=a.get('albumName'),duration_ms=a.get('durationInMillis'),isrc=a.get('isrc'),raw={'playParams':a.get('playParams')}))
        for p in playlists:
            a=p.get('attributes') or {}
            if not p.get('id'):continue
            pid=p['id']
            out['owned_playlist'].append(Item('owned_playlist',pid,a.get('name') or '',kind='playlist'))
            items=await paged(f'me/library/playlists/{pid}/tracks')
            scope=f'playlist:{pid}'
            out[scope]=[]
            occurrences={}
            for pos,t in enumerate(items):
                attr=t.get('attributes') or {}
                if not t.get('id'): continue
                occurrences[t["id"]]=occurrences.get(t["id"],0)+1
                out[scope].append(Item(scope,f"{t['id']}#{occurrences[t['id']]}",attr.get('name') or '',artist=attr.get('artistName'),album=attr.get('albumName'),duration_ms=attr.get('durationInMillis'),isrc=attr.get('isrc'),raw={'track_id':t['id'],'position':pos}))
        return out
