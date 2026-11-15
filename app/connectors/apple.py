from datetime import datetime, timezone, timedelta
import httpx
import jwt
from .base import Item, NotConfigured
from ..config import settings

API='https://api.music.apple.com/v1'

def developer_token():
    if not (settings.apple_key_path.is_file() and settings.apple_key_id and settings.apple_team_id):
        raise NotConfigured('Apple Music developer credentials missing')
    now=datetime.now(timezone.utc)
    return jwt.encode({'iss':settings.apple_team_id,'iat':int(now.timestamp()),'exp':int((now+timedelta(hours=12)).timestamp())}, settings.apple_key_path.read_text(),algorithm='ES256',headers={'kid':settings.apple_key_id})

async def fetch():
    if not settings.apple_user_token_path.is_file():
        raise NotConfigured('Apple Music user token file missing')
    user_token=settings.apple_user_token_path.read_text().strip()
    if not user_token:
        raise NotConfigured('Apple Music user token file empty')
    headers={'Authorization':f'Bearer {developer_token()}','Music-User-Token':user_token}
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
