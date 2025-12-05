from flask import Flask, render_template, request, jsonify, send_file, session, send_from_directory
import requests
import re
import os
import subprocess
import sys
from datetime import datetime, timedelta
import instaloader
import instaloader.exceptions
import uuid
import logging
import shutil
import json
import threading
import time
import base64
from io import BytesIO
from PIL import Image
import imageio
import random
import atexit
import mimetypes

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'instagram-analyzer-secret-' + str(uuid.uuid4()))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('instagram_analyzer.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MediaManager:
    def __init__(self, media_folder="static/media"):
        self.media_folder = media_folder
        self._setup_directories()
    
    def _setup_directories(self):
        os.makedirs(self.media_folder, exist_ok=True)
    
    def save_video(self, session_id: str, post_index: int, video_data: bytes) -> str:
        """Save video to local folder and return filename"""
        filename = f"{session_id}_{post_index:02d}_video.mp4"
        filepath = os.path.join(self.media_folder, filename)
        
        with open(filepath, 'wb') as f:
            f.write(video_data)
        
        logger.info(f"Saved video: {filename} ({len(video_data) // 1024} KB)")
        return filename
    
    def save_image(self, session_id: str, post_index: int, image_data: bytes, extension: str = '.jpg') -> str:
        """Save image to local folder and return filename"""
        filename = f"{session_id}_{post_index:02d}_image{extension}"
        filepath = os.path.join(self.media_folder, filename)
        
        with open(filepath, 'wb') as f:
            f.write(image_data)
        
        logger.info(f"Saved image: {filename} ({len(image_data) // 1024} KB)")
        return filename
    
    def get_media_url(self, filename: str) -> str:
        """Get URL for media file"""
        return f"/media/{filename}"
    
    def get_media_path(self, filename: str) -> str:
        """Get full path for media file"""
        return os.path.join(self.media_folder, filename)
    
    def media_exists(self, filename: str) -> bool:
        """Check if media file exists"""
        return os.path.exists(os.path.join(self.media_folder, filename))
    
    def cleanup_session_media(self, session_id: str):
        """Cleanup all media files for a session"""
        try:
            media_files = [f for f in os.listdir(self.media_folder) if f.startswith(session_id)]
            for media_file in media_files:
                file_path = os.path.join(self.media_folder, media_file)
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"Removed media file: {media_file}")
        except Exception as e:
            logger.error(f"Error cleaning media for session {session_id}: {e}")

class SessionManager:
    def __init__(self, media_manager: MediaManager):
        self.sessions_folder = "sessions"
        self.media_manager = media_manager
        self.active_sessions = {}
        self.session_locks = {}
        self._setup_directories()
        self._load_existing_sessions()
        self._start_auto_cleaner()
        logger.info("Session Manager initialized")
    
    def _setup_directories(self):
        os.makedirs(self.sessions_folder, exist_ok=True)
    
    def _load_existing_sessions(self):
        """Load existing sessions from disk on startup"""
        try:
            for session_file in os.listdir(self.sessions_folder):
                if session_file.endswith('.json'):
                    session_id = session_file[:-5]  # Remove .json extension
                    file_path = os.path.join(self.sessions_folder, session_file)
                    
                    try:
                        with open(file_path, 'r') as f:
                            session_data = json.load(f)
                        
                        # Ensure backward compatibility - add missing fields
                        if 'posts_analyzed' not in session_data:
                            session_data['posts_analyzed'] = len(session_data.get('posts', []))
                        if 'stories' not in session_data:
                            session_data['stories'] = []
                        if 'posts_downloaded' not in session_data:
                            session_data['posts_downloaded'] = len(session_data.get('posts', []))
                        
                        # Check if session is expired
                        if 'expires_at' in session_data:
                            expires_at = datetime.fromisoformat(session_data['expires_at'])
                            if datetime.now() > expires_at:
                                os.remove(file_path)
                                continue
                        
                        self.active_sessions[session_id] = session_data
                        self.session_locks[session_id] = threading.Lock()
                        logger.info(f"Loaded existing session: {session_id}")
                    except Exception as e:
                        logger.error(f"Error loading session {session_id}: {e}")
                        os.remove(file_path)
        except Exception as e:
            logger.error(f"Error loading sessions: {e}")
    
    def _start_auto_cleaner(self):
        def cleaner():
            while True:
                try:
                    self._clean_expired_sessions()
                except Exception as e:
                    logger.error(f"Auto cleaner error: {e}")
                time.sleep(60)
        
        thread = threading.Thread(target=cleaner, daemon=True)
        thread.start()
        logger.info("Auto cleaner thread started")
    
    def _clean_expired_sessions(self):
        """Clean expired sessions automatically"""
        current_time = datetime.now()
        sessions_to_remove = []
        
        for session_id, session_data in list(self.active_sessions.items()):
            if 'expires_at' in session_data:
                expires_at = datetime.fromisoformat(session_data['expires_at'])
                if current_time > expires_at:
                    sessions_to_remove.append(session_id)
        
        for session_id in sessions_to_remove:
            self.cleanup_session(session_id)
            logger.info(f"Auto-cleaned expired session: {session_id}")
    
    def create_or_get_session(self, username, request_session_id=None):
        """Create a new session or get existing one"""
        if request_session_id and request_session_id in self.active_sessions:
            # Check if session is for same user
            session_data = self.active_sessions[request_session_id]
            if session_data.get('username') == username:
                # Renew session
                session_data['expires_at'] = (datetime.now() + timedelta(minutes=30)).isoformat()
                session_data['last_accessed'] = datetime.now().isoformat()
                self._save_session_to_file(request_session_id, session_data)
                logger.info(f"Renewed existing session: {request_session_id}")
                return request_session_id
        
        # Create new session
        session_id = str(uuid.uuid4())[:12]
        session_data = {
            'session_id': session_id,
            'username': username,
            'created_at': datetime.now().isoformat(),
            'expires_at': (datetime.now() + timedelta(minutes=30)).isoformat(),
            'last_accessed': datetime.now().isoformat(),
            'status': 'created',
            'data_loaded': False,
            'profile': None,
            'posts': [],
            'stories': [],
            'analytics': {},
            'progress': 0,
            'total_posts': 0,
            'downloaded_posts': 0,
            'posts_analyzed': 0,
            'posts_downloaded': 0
        }
        
        self.active_sessions[session_id] = session_data
        self.session_locks[session_id] = threading.Lock()
        self._save_session_to_file(session_id, session_data)
        
        logger.info(f"Created new session: {session_id} for user: {username}")
        return session_id
    
    def _save_session_to_file(self, session_id, session_data):
        """Save session data to JSON file"""
        try:
            session_file = os.path.join(self.sessions_folder, f"{session_id}.json")
            with open(session_file, 'w') as f:
                json.dump(session_data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving session file {session_id}: {e}")
    
    def update_session_data(self, session_id, data_update):
        """Update session data"""
        if session_id not in self.active_sessions:
            return False
        
        try:
            with self.session_locks.get(session_id, threading.Lock()):
                # Update in-memory
                self.active_sessions[session_id].update(data_update)
                self.active_sessions[session_id]['last_accessed'] = datetime.now().isoformat()
                self.active_sessions[session_id]['expires_at'] = (datetime.now() + timedelta(minutes=30)).isoformat()
                
                # Update on disk
                self._save_session_to_file(session_id, self.active_sessions[session_id])
                
                logger.debug(f"Updated session: {session_id}")
                return True
        except Exception as e:
            logger.error(f"Error updating session {session_id}: {e}")
            return False
    
    def get_session(self, session_id):
        """Get session data"""
        if session_id in self.active_sessions:
            session_data = self.active_sessions[session_id]
            
            # Check expiration
            if 'expires_at' in session_data:
                expires_at = datetime.fromisoformat(session_data['expires_at'])
                if datetime.now() > expires_at:
                    self.cleanup_session(session_id)
                    return None
            
            # Renew session on access
            session_data['last_accessed'] = datetime.now().isoformat()
            session_data['expires_at'] = (datetime.now() + timedelta(minutes=30)).isoformat()
            self._save_session_to_file(session_id, session_data)
            
            return session_data
        
        # Try to load from file
        session_file = os.path.join(self.sessions_folder, f"{session_id}.json")
        if os.path.exists(session_file):
            try:
                with open(session_file, 'r') as f:
                    session_data = json.load(f)
                
                # Check expiration
                if 'expires_at' in session_data:
                    expires_at = datetime.fromisoformat(session_data['expires_at'])
                    if datetime.now() > expires_at:
                        os.remove(session_file)
                        return None
                
                # Ensure backward compatibility
                if 'posts_analyzed' not in session_data:
                    session_data['posts_analyzed'] = len(session_data.get('posts', []))
                
                # Load into memory
                self.active_sessions[session_id] = session_data
                self.session_locks[session_id] = threading.Lock()
                
                logger.info(f"Loaded session from disk: {session_id}")
                return session_data
                
            except Exception as e:
                logger.error(f"Error loading session file {session_id}: {e}")
                return None
        
        return None
    
    def cleanup_session(self, session_id):
        """Cleanup a session"""
        try:
            logger.info(f"Cleaning up session: {session_id}")
            
            # Remove from memory
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
            
            if session_id in self.session_locks:
                del self.session_locks[session_id]
            
            # Remove session file
            session_file = os.path.join(self.sessions_folder, f"{session_id}.json")
            if os.path.exists(session_file):
                os.remove(session_file)
            
            # Cleanup media files for this session
            self.media_manager.cleanup_session_media(session_id)
            
            logger.info(f"Successfully cleaned session: {session_id}")
            return True
        except Exception as e:
            logger.error(f"Error cleaning session {session_id}: {e}")
            return False

class InstagramAnalyzer:
    def __init__(self, session_manager: SessionManager, media_manager: MediaManager):
        self.session_manager = session_manager
        self.media_manager = media_manager
        self.instaloader_ok = self._check_instaloader()
        logger.info(f"Instagram Analyzer initialized")
    
    def _check_instaloader(self) -> bool:
        try:
            import instaloader
            # Test if we can create an instance
            L = instaloader.Instaloader(sleep=False, quiet=True)
            return True
        except ImportError:
            logger.error("instaloader not installed. Please run: pip install instaloader")
            return False
        except Exception as e:
            logger.error(f"Instaloader check failed: {e}")
            return False
    
    def _extract_username(self, url: str) -> str:
        """Extract username from Instagram URL"""
        patterns = [
            r'instagram\.com/([A-Za-z0-9_.]+)/?$',
            r'instagram\.com/([A-Za-z0-9_.]+)/\?',
            r'instagram\.com/([A-Za-z0-9_.]+)/reels',
            r'instagram\.com/([A-Za-z0-9_.]+)/posts'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                username = match.group(1)
                # Remove any trailing slashes or query params
                username = username.split('?')[0].split('/')[0]
                return username.lower()
        
        # If no pattern matched, try to extract from the end of URL
        if 'instagram.com/' in url:
            parts = url.split('instagram.com/')
            if len(parts) > 1:
                username = parts[1].split('/')[0].split('?')[0]
                return username.lower()
        
        return None
    
    def _create_instaloader(self):
        """Create and configure instaloader instance"""
        L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{shortcode}",
            quiet=True,
            max_connection_attempts=3,
            request_timeout=30.0,
            sleep=False  # We'll handle our own delays
        )
        
        # Set custom user agent
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0'
        ]
        
        L.context._session.headers.update({
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })
        
        return L
    
    def _generate_base64_thumbnail(self, video_path: str) -> str:
        """Generate base64 thumbnail for video"""
        try:
            reader = imageio.get_reader(video_path, 'ffmpeg')
            for i in range(5, 30, 5):
                try:
                    frame = reader.get_data(i)
                    image = Image.fromarray(frame)
                    image = image.resize((400, 400), Image.LANCZOS)
                    
                    # Convert to base64
                    buffered = BytesIO()
                    image.save(buffered, format="JPEG", quality=85)
                    img_str = base64.b64encode(buffered.getvalue()).decode()
                    reader.close()
                    return f"data:image/jpeg;base64,{img_str}"
                except:
                    continue
            reader.close()
        except Exception as e:
            logger.warning(f"Thumbnail generation failed: {e}")
        
        # Return placeholder
        return "https://via.placeholder.com/400x400/667eea/ffffff?text=Video+Thumbnail"
    
    def _image_to_base64(self, image_data: bytes) -> str:
        """Convert image bytes to base64"""
        try:
            img = Image.open(BytesIO(image_data))
            img = img.resize((400, 400), Image.LANCZOS)
            buffered = BytesIO()
            
            if img.mode in ('RGBA', 'LA', 'P'):
                # Convert to RGB for JPEG
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                rgb_img.save(buffered, format="JPEG", quality=90)
            else:
                img.save(buffered, format="JPEG", quality=90)
            
            img_str = base64.b64encode(buffered.getvalue()).decode()
            return f"data:image/jpeg;base64,{img_str}"
        except Exception as e:
            logger.warning(f"Image to base64 failed: {e}")
            return "https://via.placeholder.com/400x400/764ba2/ffffff?text=Image"
    
    def _download_with_retry(self, url: str, max_retries: int = 3) -> bytes:
        """Download content with retry logic"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.instagram.com/',
            'Origin': 'https://www.instagram.com',
            'DNT': '1',
        }
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # Exponential backoff
                    time.sleep(2 ** attempt)
                
                response = requests.get(url, headers=headers, stream=True, timeout=30)
                response.raise_for_status()
                
                # Read content with progress tracking
                content = b''
                total_size = int(response.headers.get('content-length', 0))
                chunk_size = 8192
                
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        content += chunk
                        # Break if content too large (> 50MB)
                        if len(content) > 50 * 1024 * 1024:
                            logger.warning(f"Content too large, breaking early")
                            break
                
                if content:
                    return content
                
            except requests.exceptions.Timeout:
                logger.warning(f"Download timeout (attempt {attempt + 1}/{max_retries})")
                continue
            except requests.exceptions.RequestException as e:
                logger.warning(f"Download error (attempt {attempt + 1}/{max_retries}): {e}")
                continue
        
        return None
    
    def _get_media_urls(self, post) -> tuple:
        """Get media URLs from post"""
        if post.is_video:
            # Try to get video URL
            video_url = None
            if hasattr(post, 'video_url') and post.video_url:
                video_url = post.video_url
            elif hasattr(post, 'url') and post.url:
                video_url = post.url
            
            return (video_url, None)
        else:
            # Try to get image URL
            image_url = None
            if hasattr(post, 'url') and post.url:
                image_url = post.url
            
            return (None, image_url)
    
    def download_profile_picture(self, session_id: str, profile) -> str:
        """Download profile picture"""
        try:
            if hasattr(profile, 'profile_pic_url') and profile.profile_pic_url:
                logger.info(f"Downloading profile picture for {profile.username}")
                image_data = self._download_with_retry(profile.profile_pic_url)
                
                if image_data:
                    filename = self.media_manager.save_image(
                        session_id, 
                        "profile_pic",  # Special name for profile picture
                        image_data,
                        '.jpg'
                    )
                    return self.media_manager.get_media_url(filename)
        except Exception as e:
            logger.error(f"Error downloading profile picture: {e}")
        return None
    
    def download_stories(self, session_id: str, username: str) -> list:
        """Download stories for a profile"""
        stories_data = []
        try:
            L = self._create_instaloader()
            profile = instaloader.Profile.from_username(L.context, username)
            
            # Get stories
            stories = L.get_stories(userids=[profile.userid])
            
            story_count = 0
            for story in stories:
                for item in story.get_items():
                    if story_count >= 3:  # Limit to 3 stories
                        break
                        
                    story_info = {
                        'type': 'video' if item.is_video else 'image',
                        'timestamp': item.date_utc.isoformat() if hasattr(item.date_utc, 'isoformat') else item.date.isoformat(),
                        'view_count': item.video_view_count if hasattr(item, 'video_view_count') else None,
                        'order': story_count + 1
                    }
                    
                    # Download story media
                    if item.is_video and hasattr(item, 'video_url'):
                        video_data = self._download_with_retry(item.video_url)
                        if video_data:
                            filename = self.media_manager.save_video(
                                session_id,
                                f"story_{story_count}",
                                video_data
                            )
                            story_info['media_url'] = self.media_manager.get_media_url(filename)
                            story_info['thumbnail'] = self._generate_base64_thumbnail(
                                self.media_manager.get_media_path(filename)
                            )
                    elif hasattr(item, 'url'):
                        image_data = self._download_with_retry(item.url)
                        if image_data:
                            filename = self.media_manager.save_image(
                                session_id,
                                f"story_{story_count}",
                                image_data,
                                '.jpg'
                            )
                            story_info['media_url'] = self.media_manager.get_media_url(filename)
                            story_info['thumbnail'] = self._image_to_base64(image_data)
                    
                    if 'media_url' in story_info:
                        stories_data.append(story_info)
                        story_count += 1
                        
                    if story_count >= 3:
                        break
                        
        except Exception as e:
            logger.error(f"Error downloading stories: {e}")
        
        return stories_data
    
    def analyze_and_download_profile(self, session_id: str, username: str, limit: int = 12) -> dict:
        """Analyze profile and download posts, profile picture, and stories"""
        try:
            # Update session status
            self.session_manager.update_session_data(session_id, {
                'status': 'processing',
                'data_loaded': False,
                'progress': 0,
                'total_posts': limit,
                'downloaded_posts': 0,
                'posts_analyzed': 0
            })
            
            if not self.instaloader_ok:
                return {'success': False, 'error': 'Instaloader not available'}
            
            L = self._create_instaloader()
            
            try:
                profile = instaloader.Profile.from_username(L.context, username)
                
                if profile.is_private:
                    return {'success': False, 'error': 'Profile is private'}
                
                # Download profile picture
                profile_pic_url_local = self.download_profile_picture(session_id, profile)
                
                profile_data = {
                    'username': profile.username,
                    'full_name': profile.full_name or profile.username,
                    'biography': profile.biography or 'No biography',
                    'followers': profile.followers,
                    'followees': profile.followees,
                    'posts_count': profile.mediacount,
                    'is_private': profile.is_private,
                    'is_verified': profile.is_verified,
                    'profile_pic_url': profile_pic_url_local or profile.profile_pic_url or 'https://via.placeholder.com/150/667eea/ffffff?text=IG',
                    'external_url': profile.external_url or '',
                    'profile_pic_downloaded': profile_pic_url_local is not None
                }
                
            except instaloader.exceptions.ProfileNotExistsException:
                return {'success': False, 'error': 'Profile does not exist'}
            except instaloader.exceptions.ConnectionException as e:
                return {'success': False, 'error': f'Connection error: {str(e)}'}
            except Exception as e:
                return {'success': False, 'error': f'Error loading profile: {str(e)}'}
            
            # Download stories (in background)
            stories_data = []
            def download_stories_background():
                try:
                    stories = self.download_stories(session_id, username)
                    self.session_manager.update_session_data(session_id, {
                        'stories': stories,
                        'stories_count': len(stories)
                    })
                    logger.info(f"Downloaded {len(stories)} stories for {username}")
                except Exception as e:
                    logger.error(f"Failed to download stories: {e}")
            
            story_thread = threading.Thread(target=download_stories_background, daemon=True)
            story_thread.start()
            
            posts_data = []
            post_count = 0
            
            try:
                # Get posts iterator
                posts = profile.get_posts()
                
                for post in posts:
                    if post_count >= limit:
                        break
                    
                    try:
                        post_index = post_count + 1
                        
                        # Update progress
                        self.session_manager.update_session_data(session_id, {
                            'progress': int((post_count / limit) * 100),
                            'downloaded_posts': post_count,
                            'posts_analyzed': post_count
                        })
                        
                        # Get media URLs
                        video_url, image_url = self._get_media_urls(post)
                        
                        post_info = {
                            'shortcode': post.shortcode,
                            'instagram_url': f'https://instagram.com/p/{post.shortcode}/',
                            'caption': (post.caption or '')[:200],  # Shorter caption for speed
                            'likes': post.likes or 0,
                            'comments': post.comments or 0,
                            'timestamp': post.date_utc.isoformat() if hasattr(post.date_utc, 'isoformat') else post.date.isoformat(),
                            'is_video': post.is_video,
                            'video_view_count': post.video_view_count if post.is_video else 0,
                            'engagement_rate': round(((post.likes + post.comments) / profile.followers) * 100, 2) if profile.followers > 0 else 0,
                            'order': post_index,
                            'thumbnail': '',
                            'download_url': '',
                            'media_url': '',
                            'media_filename': ''
                        }
                        
                        if post.is_video and video_url:
                            # Download video
                            logger.info(f"Downloading video {post_index}/{limit}: {post.shortcode}")
                            video_data = self._download_with_retry(video_url)
                            
                            if video_data and len(video_data) > 0:
                                # Save video locally
                                video_filename = self.media_manager.save_video(session_id, post_index, video_data)
                                
                                # Generate thumbnail
                                try:
                                    thumbnail_base64 = self._generate_base64_thumbnail(
                                        self.media_manager.get_media_path(video_filename)
                                    )
                                except:
                                    thumbnail_base64 = "https://via.placeholder.com/400x400/667eea/ffffff?text=Video"
                                
                                post_info['thumbnail'] = thumbnail_base64
                                post_info['download_url'] = self.media_manager.get_media_url(video_filename)
                                post_info['media_url'] = self.media_manager.get_media_url(video_filename)
                                post_info['media_filename'] = video_filename
                                post_info['media_type'] = 'video'
                            else:
                                post_info['thumbnail'] = "https://via.placeholder.com/400x400/667eea/ffffff?text=Video+Error"
                                post_info['media_type'] = 'video_error'
                        
                        elif not post.is_video and image_url:
                            # Download image
                            logger.info(f"Downloading image {post_index}/{limit}: {post.shortcode}")
                            image_data = self._download_with_retry(image_url)
                            
                            if image_data and len(image_data) > 0:
                                # Determine image extension
                                extension = '.jpg'
                                if '.png' in image_url.lower():
                                    extension = '.png'
                                elif '.webp' in image_url.lower():
                                    extension = '.webp'
                                
                                # Save image locally
                                image_filename = self.media_manager.save_image(session_id, post_index, image_data, extension)
                                
                                # Generate thumbnail
                                thumbnail_base64 = self._image_to_base64(image_data)
                                
                                post_info['thumbnail'] = thumbnail_base64
                                post_info['download_url'] = self.media_manager.get_media_url(image_filename)
                                post_info['media_url'] = self.media_manager.get_media_url(image_filename)
                                post_info['media_filename'] = image_filename
                                post_info['media_type'] = 'image'
                            else:
                                post_info['thumbnail'] = "https://via.placeholder.com/400x400/764ba2/ffffff?text=Image+Error"
                                post_info['media_type'] = 'image_error'
                        else:
                            # No media URL found
                            post_info['thumbnail'] = "https://via.placeholder.com/400x400/999999/ffffff?text=No+Media"
                            post_info['media_type'] = 'no_media'
                        
                        posts_data.append(post_info)
                        post_count += 1
                        
                        # Add delay between posts to avoid rate limiting
                        if post_count < limit:
                            time.sleep(random.uniform(1.5, 3.0))
                        
                    except Exception as e:
                        logger.error(f"Error processing post {post.shortcode if hasattr(post, 'shortcode') else 'unknown'}: {e}")
                        # Add error post info
                        error_post = {
                            'shortcode': f'error_{post_count}',
                            'instagram_url': '#',
                            'caption': f'Error loading post: {str(e)[:100]}',
                            'likes': 0,
                            'comments': 0,
                            'timestamp': datetime.now().isoformat(),
                            'is_video': False,
                            'video_view_count': 0,
                            'engagement_rate': 0,
                            'order': post_count + 1,
                            'thumbnail': "https://via.placeholder.com/400x400/ff4444/ffffff?text=Error",
                            'download_url': '#',
                            'media_url': '#',
                            'media_type': 'error'
                        }
                        posts_data.append(error_post)
                        post_count += 1
                        continue
                
            except Exception as e:
                logger.error(f"Error in download process: {e}")
                return {'success': False, 'error': f'Download error: {str(e)}'}
            
            if not posts_data:
                return {'success': False, 'error': 'No posts could be downloaded'}
            
            # Get stories data from session
            session_data = self.session_manager.get_session(session_id)
            if session_data:
                stories_data = session_data.get('stories', [])
            
            # Calculate analytics
            analytics = self._calculate_analytics(profile_data, posts_data)
            
            # Update session with complete data
            self.session_manager.update_session_data(session_id, {
                'status': 'completed',
                'data_loaded': True,
                'profile': profile_data,
                'posts': posts_data,
                'stories': stories_data,
                'analytics': analytics,
                'posts_analyzed': len(posts_data),
                'posts_downloaded': len(posts_data),
                'progress': 100,
                'downloaded_posts': len(posts_data),
                'total_posts': len(posts_data)
            })
            
            logger.info(f"✓ Session {session_id} completed: {len(posts_data)} posts downloaded, {len(stories_data)} stories")
            
            return {
                'success': True,
                'session_id': session_id,
                'username': username,
                'posts_analyzed': len(posts_data),
                'posts_downloaded': len(posts_data),
                'profile': profile_data,
                'posts': posts_data,
                'stories': stories_data,
                'analytics': analytics,
                'message': f'✅ Downloaded {len(posts_data)} posts and {len(stories_data)} stories'
            }
            
        except Exception as e:
            logger.error(f"Analyze and download error for session {session_id}: {e}")
            
            self.session_manager.update_session_data(session_id, {
                'status': 'failed',
                'error': str(e)
            })
            return {'success': False, 'error': f'Failed to analyze profile: {str(e)}'}
    
    def _calculate_analytics(self, profile, posts: list) -> dict:
        if not posts:
            return {}
        
        # Filter out error posts
        valid_posts = [p for p in posts if p.get('media_type') not in ['error', 'no_media']]
        
        if not valid_posts:
            return {
                'total_posts_analyzed': len(posts),
                'average_likes_per_post': 0,
                'average_comments_per_post': 0,
                'average_engagement_rate': 0,
                'total_engagement': 0,
                'video_posts_count': 0,
                'image_posts_count': 0,
                'engagement_per_follower': 0
            }
        
        total_likes = sum(p.get('likes', 0) for p in valid_posts)
        total_comments = sum(p.get('comments', 0) for p in valid_posts)
        total_engagement_rates = sum(p.get('engagement_rate', 0) for p in valid_posts)
        
        video_posts = [p for p in valid_posts if p.get('is_video', False)]
        image_posts = [p for p in valid_posts if not p.get('is_video', False)]
        
        return {
            'total_posts_analyzed': len(posts),
            'average_likes_per_post': round(total_likes / len(valid_posts), 1) if valid_posts else 0,
            'average_comments_per_post': round(total_comments / len(valid_posts), 1) if valid_posts else 0,
            'average_engagement_rate': round(total_engagement_rates / len(valid_posts), 2) if valid_posts else 0,
            'total_engagement': total_likes + total_comments,
            'video_posts_count': len(video_posts),
            'image_posts_count': len(image_posts),
            'engagement_per_follower': round(
                ((total_likes + total_comments) / profile.get('followers', 1)) * 100, 4
            ) if profile.get('followers', 0) > 0 else 0
        }

# Initialize managers
media_manager = MediaManager()
session_manager = SessionManager(media_manager)
analyzer = InstagramAnalyzer(session_manager, media_manager)

# Cleanup on exit
def cleanup_on_exit():
    logger.info("Application shutting down, cleaning up...")
    # Cleanup temp folder
    if os.path.exists("temp"):
        shutil.rmtree("temp", ignore_errors=True)

atexit.register(cleanup_on_exit)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze-profile', methods=['POST'])
def analyze_profile():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'})
        
        url = data.get('url', '').strip()
        request_session_id = data.get('session_id')
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Invalid Instagram URL'})
        
        username = analyzer._extract_username(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username from URL'})
        
        logger.info(f"Analyzing profile: {username}, session_id: {request_session_id}")
        
        # Create or get session
        session_id = session_manager.create_or_get_session(username, request_session_id)
        if not session_id:
            return jsonify({'success': False, 'error': 'Failed to create session'})
        
        # Check if session already has data
        session_data = session_manager.get_session(session_id)
        if session_data and session_data.get('data_loaded') and session_data.get('status') == 'completed':
            logger.info(f"Returning cached data for session: {session_id}")
            return jsonify({
                'success': True,
                'session_id': session_id,
                'username': username,
                'posts_analyzed': session_data.get('posts_analyzed', 0),
                'posts_downloaded': len(session_data.get('posts', [])),
                'profile': session_data.get('profile'),
                'posts': session_data.get('posts'),
                'stories': session_data.get('stories', []),
                'analytics': session_data.get('analytics'),
                'message': '✅ Using cached session data'
            })
        
        # Start analysis in background thread
        def analyze_in_background():
            try:
                result = analyzer.analyze_and_download_profile(session_id, username, limit=12)
                logger.info(f"Background analysis completed for session {session_id}: {result.get('success', False)}")
            except Exception as e:
                logger.error(f"Background analysis error for session {session_id}: {e}")
        
        # Start background thread
        thread = threading.Thread(target=analyze_in_background, daemon=True)
        thread.start()
        
        # Return immediate response with session ID
        return jsonify({
            'success': True,
            'session_id': session_id,
            'message': 'Analysis started in background. Please wait...',
            'status': 'processing'
        })
            
    except Exception as e:
        logger.error(f"Analyze error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/get-session-data/<session_id>')
def get_session_data(session_id):
    """Get session data for frontend polling"""
    try:
        session_data = session_manager.get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Session not found or expired'})
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'status': session_data.get('status'),
            'data_loaded': session_data.get('data_loaded'),
            'profile': session_data.get('profile'),
            'posts': session_data.get('posts'),
            'stories': session_data.get('stories', []),
            'analytics': session_data.get('analytics'),
            'posts_analyzed': session_data.get('posts_analyzed', 0),
            'expires_at': session_data.get('expires_at'),
            'last_accessed': session_data.get('last_accessed'),
            'progress': session_data.get('progress', 0),
            'downloaded_posts': session_data.get('downloaded_posts', 0),
            'total_posts': session_data.get('total_posts', 0),
            'message': session_data.get('error') if session_data.get('status') == 'failed' else 'OK'
        })
    except Exception as e:
        logger.error(f"Get session error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/media/<path:filename>')
def media_files(filename):
    """Serve media files from local folder"""
    try:
        filepath = media_manager.get_media_path(filename)
        if not os.path.exists(filepath):
            return "File not found", 404
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(filepath)
        if not mime_type:
            mime_type = 'application/octet-stream'
        
        # Serve file with proper headers
        response = send_file(
            filepath,
            mimetype=mime_type,
            as_attachment=False,  # False = stream, True = download
            download_name=filename
        )
        
        # Add caching headers
        response.headers['Cache-Control'] = 'public, max-age=86400'  # 1 day cache
        
        return response
        
    except Exception as e:
        logger.error(f"Media serving error: {e}")
        return "File not found", 404

@app.route('/cleanup-session/<session_id>', methods=['POST'])
def cleanup_session_route(session_id):
    try:
        success = session_manager.cleanup_session(session_id)
        if success:
            return jsonify({'success': True, 'message': 'Session cleaned up'})
        return jsonify({'success': False, 'error': 'Failed to clean session'})
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/check-session/<session_id>')
def check_session(session_id):
    """Check if session is still valid"""
    try:
        session_data = session_manager.get_session(session_id)
        if session_data:
            expires_at = datetime.fromisoformat(session_data.get('expires_at', datetime.now().isoformat()))
            minutes_left = max(0, (expires_at - datetime.now()).total_seconds() / 60)
            
            return jsonify({
                'success': True,
                'valid': True,
                'expires_at': session_data.get('expires_at'),
                'minutes_left': round(minutes_left, 1),
                'status': session_data.get('status')
            })
        return jsonify({'success': True, 'valid': False})
    except Exception as e:
        logger.error(f"Check session error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Periodic media cleanup
def cleanup_old_media():
    """Cleanup media files older than 1 day"""
    try:
        media_folder = media_manager.media_folder
        if os.path.exists(media_folder):
            current_time = time.time()
            for filename in os.listdir(media_folder):
                file_path = os.path.join(media_folder, filename)
                if os.path.isfile(file_path):
                    file_age = current_time - os.path.getmtime(file_path)
                    if file_age > 24 * 3600:  # 1 day
                        try:
                            os.remove(file_path)
                            logger.info(f"Cleaned up old media file: {filename}")
                        except:
                            pass
    except Exception as e:
        logger.error(f"Media cleanup error: {e}")

# Start cleanup thread
def start_media_cleaner():
    def cleaner():
        while True:
            cleanup_old_media()
            time.sleep(6 * 3600)  # Run every 6 hours
    
    thread = threading.Thread(target=cleaner, daemon=True)
    thread.start()

def install_requirements():
    """Install required packages if missing"""
    required_packages = [
        'instaloader>=4.10',
        'Pillow>=10.0.0',
        'imageio>=2.31.0',
        'imageio-ffmpeg>=0.4.8',
        'requests>=2.31.0',
        'flask>=2.3.0'
    ]
    
    for package in required_packages:
        package_name = package.split('>=')[0]
        try:
            __import__(package_name.replace('-', '_'))
            logger.info(f"✓ {package_name} is already installed")
        except ImportError:
            logger.info(f"Installing {package_name}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])
                logger.info(f"✓ Successfully installed {package_name}")
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install {package_name}: {e}")

if __name__ == '__main__':
    # Install required packages
    install_requirements()
    
    # Create directories
    os.makedirs("sessions", exist_ok=True)
    os.makedirs("static/media", exist_ok=True)
    
    # Start media cleaner
    start_media_cleaner()
    
    print("=" * 60)
    print("Instagram Profile Analyzer")
    print("Optimized for Speed - No Timeout")
    print("=" * 60)
    print(f"Sessions folder: {os.path.abspath(session_manager.sessions_folder)}")
    print(f"Media folder: {os.path.abspath(media_manager.media_folder)}")
    print("=" * 60)
    print("Server starting on http://localhost:5000")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)