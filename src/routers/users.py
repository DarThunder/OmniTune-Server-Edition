# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import asyncio
from pathlib import Path
import io
import re
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, status, Query
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from PIL import Image

from core.audit import log_action
from core.database import get_db
from core.configs import PROFILES_PATH
from models.db import Favorite, GlobalMedia, Playlist, PlaylistItem, User
from models.schemas import PlaylistCreate, PlaylistOrder, PlaylistUpdate, UserProfileUpdate
from services import media_repo
from routers.dependencies import get_current_user

MAX_PFP_SIZE = 5 * 1024 * 1024

router = APIRouter(
    prefix="/users/me",
    tags=["Users"],
    dependencies=[Depends(get_current_user)]
)

def _save_proccesed_image(img_bytes: bytes, save_path: Path):
    image = Image.open(io.BytesIO(img_bytes))
    image.save(save_path, format="WEBP", quality=85)

def _safe_user_dir(username: str) -> tuple[str, Path]:
    safe_username = re.sub(r"[^a-zA-Z0-9_-]", "", username)
    if not safe_username:
        raise HTTPException(status_code=400, detail="Nombre de usuario inválido para archivo")
    profiles_dir = Path(PROFILES_PATH)
    return safe_username, profiles_dir / safe_username

def _covers_dir(username: str) -> Path:
    _, user_dir = _safe_user_dir(username)
    return user_dir / "playlists"

def _ensure_media_exists(db: Session, v_hash: str) -> None:
    if not db.query(GlobalMedia.hash).filter(GlobalMedia.hash == v_hash).first():
        raise HTTPException(status_code=404, detail="Contenido no encontrado")

def _resolve_playlist_cover(playlist: Playlist) -> str | None:
    if playlist.custom_cover:
        return f"/users/me/playlists/{playlist.id}/cover"
    
    if playlist.items:
        return f"/thumbnail/{playlist.items[0].media_hash}"

    return None
 
def _get_playlist_or_404(db: Session, user: User, playlist_id: int) -> Playlist:
    playlist = db.query(Playlist).filter(
        Playlist.id == playlist_id,
        Playlist.user_id == user.id
    ).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist no encontrada")
    return playlist

@router.get("/profile")
def get_profile(current_user: User = Depends(get_current_user)):
    return {
        "username": current_user.username,
        "bio": current_user.bio,
    }

@router.patch("/profile")
def update_profile(
    profile_data: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if profile_data.bio is not None:
        current_user.bio = profile_data.bio
        
    db.commit()
    db.refresh(current_user)

    log_action(db, current_user.id, "UPDATE_PROFILE", "Actualizó biografía")
    return {
        "message": "Perfil actualizado exitosamente",
        "username": current_user.username,
        "bio": current_user.bio
    }

@router.post("/pfp")
async def upload_pfp(
    file: UploadFile = File(...), 
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")
        
    try:
        content = await file.read(MAX_PFP_SIZE + 1)
        if len(content) > MAX_PFP_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, 
                detail="El archivo es demasiado grande (Máximo 5MB)"
            )
            
        safe_username, user_dir = _safe_user_dir(current_user.username)
        user_dir.mkdir(parents=True, exist_ok=True)
        save_path = user_dir / f"{safe_username}.webp"
        
        await asyncio.to_thread(_save_proccesed_image, content, save_path)
        log_action(db, current_user.id, "UPDATE_PFP", "Actualizó foto de perfil")
        return {"message": "Foto de perfil actualizada exitosamente"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando la imagen: {str(e)}")

@router.get("/pfp")
def get_pfp(current_user: User = Depends(get_current_user)):
    profiles_dir = Path(PROFILES_PATH)
    safe_username, user_dir = _safe_user_dir(current_user.username)
    user_pfp = user_dir / f"{safe_username}.webp"
    default_pfp = profiles_dir / "default.webp"
    
    if user_pfp.exists():
        return FileResponse(user_pfp, media_type="image/webp")
    elif default_pfp.exists():
        return FileResponse(default_pfp, media_type="image/webp")
    else:
        raise HTTPException(status_code=404, detail="Falta el archivo default.webp en el servidor")

@router.get("/favorites")
def get_favorites(
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    favoritos_db = db.query(Favorite).filter(Favorite.user_id == current_user.id).all()
    
    hashes = [fav.media_hash for fav in favoritos_db]
    metas = media_repo.get_metadata_many(hashes)
    videos = [metas[h] for h in hashes if h in metas]
    return {"favorites": videos}

@router.post("/favorites/{v_hash}")
def add_favorite(
    v_hash: str, 
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    _ensure_media_exists(db, v_hash)
    existing_fav = db.query(Favorite).filter(
        Favorite.user_id == current_user.id, 
        Favorite.media_hash == v_hash
    ).first()
    
    if not existing_fav:
        new_fav = Favorite(user_id=current_user.id, media_hash=v_hash)
        db.add(new_fav)
        db.commit()
        db.refresh(current_user)
        
    updated_favorites = [fav.media_hash for fav in current_user.favorites]
    log_action(db, current_user.id, "ADD_FAVORITE", f"Video Hash: {v_hash}")
    return {"message": "Video añadido a favoritos", "favorites": updated_favorites}

@router.delete("/favorites/{v_hash}")
def remove_favorite(
    v_hash: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fav = db.query(Favorite).filter(
        Favorite.user_id == current_user.id,
        Favorite.media_hash == v_hash,
    ).first()
    if fav:
        db.delete(fav)
        db.commit()
    log_action(db, current_user.id, "REMOVE_FAVORITE", f"Hash: {v_hash}")
    return {"message": "Quitado de favoritos"}

@router.get("/playlists")
def list_playlists(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    playlists = db.query(Playlist)\
                  .options(joinedload(Playlist.items))\
                  .filter(Playlist.user_id == current_user.id)\
                  .all()
 
    result = [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description or "",
            "cover_url": _resolve_playlist_cover(p),
            "item_count": len(p.items)
        }
        for p in playlists
    ]
    return {"playlists": result}
 
 
@router.post("/playlists")
def create_empty_playlist(
    data: PlaylistCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
 
    existing = db.query(Playlist).filter(
        Playlist.user_id == current_user.id,
        Playlist.name == name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="La playlist ya existe")
 
    new_playlist = Playlist(
        user_id=current_user.id,
        name=name,
        description=data.description
    )
    db.add(new_playlist)
    db.commit()
    db.refresh(new_playlist)
 
    log_action(db, current_user.id, "CREATE_PLAYLIST", f"Nombre: {name}")
    return {
        "message": f"Playlist '{name}' creada exitosamente",
        "id": str(new_playlist.id),
        "name": new_playlist.name,
        "description": new_playlist.description or ""
    }
 
@router.get("/playlists/{playlist_id}")
def get_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
 
    playlist_items = db.query(PlaylistItem)\
                       .filter(PlaylistItem.playlist_id == playlist.id)\
                       .order_by(PlaylistItem.position)\
                       .all()
 
    hashes = [it.media_hash for it in playlist_items]
    metas = media_repo.get_metadata_many(hashes)

    items = []
    for it in playlist_items:
        meta = metas.get(it.media_hash)
        if meta is None:
            continue
        items.append({**meta, "media_type": "audio"})
 
    return {
        "id": str(playlist.id),
        "name": playlist.name,
        "description": playlist.description or "",
        "cover_url": _resolve_playlist_cover(playlist),
        "item_count": len(items),
        "items": items
    }

@router.patch("/playlists/{playlist_id}")
def update_playlist_metadata(
    playlist_id: int,
    data: PlaylistUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
 
    if data.name is not None:
        new_name = data.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
        if new_name != playlist.name:
            clash = db.query(Playlist).filter(
                Playlist.user_id == current_user.id,
                Playlist.name == new_name,
                Playlist.id != playlist.id
            ).first()
            if clash:
                raise HTTPException(status_code=400, detail="Ya tienes una playlist con ese nombre")
            playlist.name = new_name
 
    if data.description is not None:
        playlist.description = data.description.strip()
 
    db.commit()
    db.refresh(playlist)
 
    log_action(db, current_user.id, "UPDATE_PLAYLIST", f"Actualizó playlist {playlist.id} ({playlist.name})")
    return {
        "message": "Playlist actualizada exitosamente",
        "id": str(playlist.id),
        "name": playlist.name,
        "description": playlist.description or ""
    }
 
 
@router.delete("/playlists/{playlist_id}")
def delete_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
    name = playlist.name
    cover_name = playlist.custom_cover
 
    db.delete(playlist)
    db.commit()
 
    if cover_name:
        try:
            (_covers_dir(current_user.username) / cover_name).unlink(missing_ok=True)
        except OSError:
            pass
 
    log_action(db, current_user.id, "DELETE_PLAYLIST", f"Playlist {playlist_id} ({name})")
    return {"message": f"Playlist '{name}' eliminada exitosamente"}
 
 
@router.post("/playlists/{playlist_id}/items/{v_hash}")
def add_to_playlist(
    playlist_id: int,
    v_hash: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
 
    if not db.query(GlobalMedia.hash).filter(GlobalMedia.hash == v_hash).first():
        raise HTTPException(status_code=404, detail="Contenido no encontrado")
 
    existing_item = db.query(PlaylistItem).filter(
        PlaylistItem.playlist_id == playlist.id,
        PlaylistItem.media_hash == v_hash
    ).first()
 
    if not existing_item:
        max_pos = db.query(func.max(PlaylistItem.position))\
                    .filter(PlaylistItem.playlist_id == playlist.id)\
                    .scalar()
        next_position = 0 if max_pos is None else max_pos + 1
        db.add(PlaylistItem(
            playlist_id=playlist.id,
            media_hash=v_hash,
            position=next_position
        ))
        db.commit()
 
    log_action(db, current_user.id, "PLAYLIST_ADD", f"Playlist: {playlist.id} | Hash: {v_hash}")
    return {"message": f"Contenido añadido a la playlist '{playlist.name}'"}
 
 
@router.delete("/playlists/{playlist_id}/items/{v_hash}")
def remove_from_playlist(
    playlist_id: int,
    v_hash: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
 
    item = db.query(PlaylistItem).filter(
        PlaylistItem.playlist_id == playlist.id,
        PlaylistItem.media_hash == v_hash
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="El contenido no está en esta playlist")
 
    db.delete(item)
    db.commit()
 
    log_action(db, current_user.id, "PLAYLIST_REMOVE", f"Playlist: {playlist.id} | Hash: {v_hash}")
    return {"message": "Contenido removido de la playlist"}
 
 
@router.post("/playlists/{playlist_id}/cover")
async def upload_playlist_cover(
    playlist_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
 
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")
 
    content = await file.read(MAX_PFP_SIZE + 1)
    if len(content) > MAX_PFP_SIZE:
        raise HTTPException(status_code=413, detail="Máximo 5MB")
 
    covers_dir = _covers_dir(current_user.username)
    covers_dir.mkdir(parents=True, exist_ok=True)
 
    filename = f"pl_{playlist.id}.webp"
    save_path = covers_dir / filename
    await asyncio.to_thread(_save_proccesed_image, content, save_path)
 
    playlist.custom_cover = filename
    db.commit()
 
    return {"message": "Portada actualizada exitosamente"}
 
 
@router.get("/playlists/{playlist_id}/cover")
def get_playlist_cover(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
    if not playlist.custom_cover:
        raise HTTPException(status_code=404, detail="Portada no encontrada")
 
    cover_file = _covers_dir(current_user.username) / playlist.custom_cover
    if not cover_file.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
 
    return FileResponse(cover_file, media_type="image/webp")
 
 
@router.delete("/playlists/{playlist_id}/cover")
def delete_playlist_cover(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
    if not playlist.custom_cover:
        raise HTTPException(status_code=404, detail="La playlist no tiene portada personalizada")
 
    cover_name = playlist.custom_cover
    playlist.custom_cover = None
    db.commit()
 
    try:
        (_covers_dir(current_user.username) / cover_name).unlink(missing_ok=True)
    except OSError:
        pass
 
    return {"message": "Portada eliminada"}

@router.put("/playlists/{playlist_id}/order")
def reorder_playlist(playlist_id: int, data: PlaylistOrder,
                     current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    playlist = _get_playlist_or_404(db, current_user, playlist_id)
    items = {i.media_hash: i for i in db.query(PlaylistItem)
             .filter(PlaylistItem.playlist_id == playlist.id)}
    for pos, h in enumerate(data.hashes):
        if h in items:
            items[h].position = pos
    db.commit()
    return {"message": "Orden actualizado"}