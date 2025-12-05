from flask import Flask, render_template, request, jsonify, send_file
import requests
import re
import os
import subprocess
import sys
from datetime import datetime
import instaloader
import uuid
import logging
import zipfile
import shutil
from typing import Dict, List, Any
import time

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class InstagramAnalyzer:
    def __init__(self):
        self.instaloader_ok = self._check_instaloader()
        self.downloads_folder = "downloads"
        self._setup_directories()

    def _setup_directories(self):
        """Create downloads directory if it doesn't exist"""
        os.makedirs(self.downloads_folder, exist_ok=True)

    def _check_instaloader(self) -> bool:
        """Check if instaloader is available"""
        try:
            import instaloader
            return True
        except ImportError:
            return False

    def install_instaloader(self) -> bool:
        """Install instaloader package"""
        try:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", 
                "instaloader", "--quiet"
            ])
            self.instaloader_ok = True
            return True
        except Exception as e:
            logger.error(f"Installation failed: {e}")
            return False

    def _extract_username(self, url: str) -> str:
        """Extract username from Instagram profile URL"""
        patterns = [
            r'instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$',
            r'instagram\.com/([A-Za-z0-9_.]+)/(?:reels|posts)/?'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1).split('?')[0]
        return None

    def _extract_shortcode(self, url: str) -> str:
        """Extract shortcode from post/reel URL"""
        patterns = [
            r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)',
            r'/stories/[^/]+/([A-Za-z0-9_-]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def _is_profile_url(self, url: str) -> bool:
        """Check if URL is a profile URL"""
        return bool(re.search(
            r'instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$', 
            url
        ))

    def _create_instaloader(self, download_media: bool = False):
        """Create configured Instaloader instance with improved settings"""
        return instaloader.Instaloader(
            download_pictures=download_media,
            download_videos=download_media,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{shortcode}",
            quiet=True,
            max_connection_attempts=3
        )

    def _calculate_engagement_rate(self, post, followers: int) -> float:
        """Calculate engagement rate for a post"""
        if followers == 0:
            return 0.0
        
        total_engagement = post.likes + post.comments
        return round((total_engagement / followers) * 100, 2)

    def _get_post_data(self, post, profile) -> Dict[str, Any]:
        """Extract post data with media URLs and thumbnail"""
        media_urls = []
        thumbnail_url = None
        
        try:
            # Get primary media
            if post.is_video:
                if hasattr(post, 'video_url') and post.video_url:
                    media_urls.append({
                        'url': post.video_url,
                        'type': 'video',
                        'extension': '.mp4'
                    })
                # Use thumbnail for video preview
                thumbnail_url = post.url if hasattr(post, 'url') else None
            else:
                if hasattr(post, 'url') and post.url:
                    media_urls.append({
                        'url': post.url,
                        'type': 'image',
                        'extension': '.jpg'
                    })
                    thumbnail_url = post.url
            
            # Handle carousel posts
            if post.mediacount > 1:
                for node in post.get_sidecar_nodes():
                    if node.is_video and hasattr(node, 'video_url'):
                        media_urls.append({
                            'url': node.video_url,
                            'type': 'video',
                            'extension': '.mp4'
                        })
                    elif hasattr(node, 'display_url'):
                        media_urls.append({
                            'url': node.display_url,
                            'type': 'image',
                            'extension': '.jpg'
                        })
            
            # Fallback thumbnail
            if not thumbnail_url:
                thumbnail_url = 'https://via.placeholder.com/300x300/e1e8ed/657786?text=Instagram+Post'
            
            return {
                'shortcode': post.shortcode,
                'url': f"https://instagram.com/p/{post.shortcode}/",
                'caption': post.caption or '',
                'likes': post.likes,
                'comments': post.comments,
                'timestamp': post.date_utc.isoformat(),
                'is_video': post.is_video,
                'video_view_count': post.video_view_count if post.is_video else 0,
                'engagement_rate': self._calculate_engagement_rate(post, profile.followers),
                'media_count': post.mediacount,
                'type': 'video' if post.is_video else 'carousel' if post.mediacount > 1 else 'image',
                'media_urls': media_urls,
                'thumbnail_url': thumbnail_url
            }
        except Exception as e:
            logger.error(f"Error processing post {post.shortcode}: {e}")
            return None

    def get_profile_info(self, username: str) -> Dict[str, Any]:
        """Get comprehensive profile information with retry logic"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                if not self.instaloader_ok:
                    return {'success': False, 'error': 'Instaloader not available'}
                
                L = self._create_instaloader()
                profile = instaloader.Profile.from_username(L.context, username)
                
                # Get latest posts
                posts = []
                for i, post in enumerate(profile.get_posts()):
                    if i >= 12:
                        break
                    post_data = self._get_post_data(post, profile)
                    if post_data:
                        posts.append(post_data)
                    time.sleep(0.5)  # Rate limiting
                
                # Calculate analytics
                analytics = self._calculate_analytics(profile, posts)
                
                return {
                    'success': True,
                    'profile': {
                        'username': profile.username,
                        'user_id': profile.userid,
                        'full_name': profile.full_name,
                        'biography': profile.biography,
                        'followers': profile.followers,
                        'followees': profile.followees,
                        'posts_count': profile.mediacount,
                        'is_private': profile.is_private,
                        'is_verified': profile.is_verified,
                        'profile_pic_url': profile.profile_pic_url,
                        'external_url': profile.external_url,
                        'is_business_account': getattr(profile, 'is_business_account', False),
                        'business_category': getattr(profile, 'business_category_name', ''),
                    },
                    'posts': posts,
                    'analytics': analytics
                }
                
            except instaloader.exceptions.ProfileNotExistsException:
                return {'success': False, 'error': f'Profile @{username} does not exist'}
            except instaloader.exceptions.PrivateProfileNotFollowedException:
                return {'success': False, 'error': f'Profile @{username} is private'}
            except instaloader.exceptions.ConnectionException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Connection error, retrying... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                return {'success': False, 'error': 'Network connection error. Please try again.'}
            except instaloader.exceptions.QueryReturnedBadRequestException:
                return {'success': False, 'error': 'Instagram is rate limiting requests. Please wait a few minutes and try again.'}
            except Exception as e:
                logger.error(f"Profile info error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return {'success': False, 'error': f'Failed to fetch profile. Please try again later.'}
        
        return {'success': False, 'error': 'Failed after multiple retries'}

    def get_profile_posts_preview(self, username: str, limit: int = 20) -> Dict[str, Any]:
        """Get profile posts for preview and selection with retry logic"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                if not self.instaloader_ok:
                    return {'success': False, 'error': 'Instaloader not available'}
                
                L = self._create_instaloader()
                profile = instaloader.Profile.from_username(L.context, username)
                
                posts = []
                for i, post in enumerate(profile.get_posts()):
                    if i >= limit:
                        break
                    post_data = self._get_post_data(post, profile)
                    if post_data:
                        posts.append(post_data)
                    time.sleep(0.3)  # Rate limiting
                
                return {
                    'success': True,
                    'profile': {
                        'username': profile.username,
                        'full_name': profile.full_name,
                        'profile_pic_url': profile.profile_pic_url,
                        'is_private': profile.is_private,
                        'is_verified': profile.is_verified,
                        'posts_count': profile.mediacount,
                        'followers': profile.followers
                    },
                    'posts': posts,
                    'total_posts_fetched': len(posts)
                }
                
            except instaloader.exceptions.ConnectionException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Connection error, retrying... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                return {'success': False, 'error': 'Network connection error. Please try again.'}
            except Exception as e:
                logger.error(f"Profile posts preview error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return {'success': False, 'error': 'Failed to load posts. Please try again.'}
        
        return {'success': False, 'error': 'Failed after multiple retries'}

    def _calculate_analytics(self, profile, posts: List[Dict]) -> Dict[str, Any]:
        """Calculate profile analytics"""
        if not posts:
            return {}
        
        total_likes = sum(p['likes'] for p in posts)
        total_comments = sum(p['comments'] for p in posts)
        avg_engagement = sum(p['engagement_rate'] for p in posts) / len(posts)
        
        video_posts = [p for p in posts if p['is_video']]
        image_posts = [p for p in posts if not p['is_video']]
        
        return {
            'total_posts_analyzed': len(posts),
            'average_likes_per_post': round(total_likes / len(posts), 1),
            'average_comments_per_post': round(total_comments / len(posts), 1),
            'average_engagement_rate': round(avg_engagement, 2),
            'total_engagement': total_likes + total_comments,
            'video_posts_count': len(video_posts),
            'image_posts_count': len(image_posts),
            'engagement_per_follower': round(
                ((total_likes + total_comments) / profile.followers) * 100, 4
            ) if profile.followers > 0 else 0
        }

    def download_selected_posts(self, username: str, shortcodes: List[str]) -> Dict[str, Any]:
        """Download selected posts and create ZIP with proper error handling"""
        download_folder = None
        try:
            if not self.instaloader_ok:
                return {'success': False, 'error': 'Instaloader not available'}
            
            if not shortcodes:
                return {'success': False, 'error': 'No posts selected'}
            
            L = self._create_instaloader(download_media=True)
            
            # Use username folder instead of temp folder
            download_folder = os.path.join(self.downloads_folder, username)
            if not os.path.exists(download_folder):
                print("already exists")
            else:
                os.makedirs(download_folder, exist_ok=True)
            
            downloaded_files = []
            failed_downloads = []
            
            # Track files before each download
            for idx, shortcode in enumerate(shortcodes):
                try:
                    logger.info(f"Downloading post {idx + 1}/{len(shortcodes)}: {shortcode}")
                    
                    # Check if this post already exists in the folder
                    existing_files = [f for f in os.listdir(download_folder) 
                                     if shortcode in f and os.path.isfile(os.path.join(download_folder, f))]
                    
                    if existing_files:
                        logger.info(f"Post {shortcode} already exists: {existing_files}")
                        # Add existing files to downloaded list
                        for file in existing_files:
                            file_path = os.path.join(download_folder, file)
                            downloaded_files.append({
                                'filename': file,
                                'path': file_path,
                                'shortcode': shortcode,
                                'size': os.path.getsize(file_path),
                                'already_existed': True
                            })
                        continue
                    
                    # Get list of files before download
                    files_before = set(os.listdir(download_folder))
                    
                    post = instaloader.Post.from_shortcode(L.context, shortcode)
                    L.download_post(post, target=download_folder)
                    
                    # Wait for download to complete
                    time.sleep(2)
                    
                    # Get list of files after download
                    files_after = set(os.listdir(download_folder))
                    
                    # Find new files
                    new_files = files_after - files_before
                    
                    if not new_files:
                        # If no new files detected, check all files containing shortcode
                        new_files = [f for f in os.listdir(download_folder) if shortcode in f]
                    
                    # Filter for media files only
                    media_extensions = ('.mp4', '.jpg', '.jpeg', '.png', '.webp')
                    found_files = []
                    
                    for file in new_files:
                        file_path = os.path.join(download_folder, file)
                        # Check if it's a file (not directory) and has size > 0
                        if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                            # Check if it's a media file (by extension or content)
                            file_lower = file.lower()
                            if any(ext in file_lower for ext in media_extensions):
                                found_files.append(file)
                    
                    logger.info(f"Found {len(found_files)} media file(s) for {shortcode}: {found_files}")
                    
                    if found_files:
                        for file_idx, file in enumerate(found_files):
                            old_path = os.path.join(download_folder, file)
                            
                            # Determine extension
                            if '.mp4' in file.lower():
                                ext = '.mp4'
                            elif '.jpg' in file.lower() or '.jpeg' in file.lower():
                                ext = '.jpg'
                            elif '.png' in file.lower():
                                ext = '.png'
                            else:
                                ext = os.path.splitext(file)[1] or '.jpg'
                            
                            # Create unique filename
                            if len(found_files) > 1:
                                new_name = f"{username}_{shortcode}_{file_idx+1}{ext}"
                            else:
                                new_name = f"{username}_{shortcode}{ext}"
                            
                            new_path = os.path.join(download_folder, new_name)
                            
                            # Only rename if the file doesn't already have the correct name
                            if old_path != new_path and not os.path.exists(new_path):
                                os.rename(old_path, new_path)
                                final_path = new_path
                            else:
                                final_path = old_path
                            
                            downloaded_files.append({
                                'filename': os.path.basename(final_path),
                                'path': final_path,
                                'shortcode': shortcode,
                                'size': os.path.getsize(final_path),
                                'already_existed': False
                            })
                            logger.info(f"Successfully processed: {os.path.basename(final_path)} ({os.path.getsize(final_path)} bytes)")
                    else:
                        failed_downloads.append(shortcode)
                        logger.warning(f"No media files found for {shortcode} in {download_folder}")
                        # Log all files in folder for debugging
                        logger.info(f"Files in folder: {os.listdir(download_folder)}")
                    
                    time.sleep(0.5)  # Rate limiting
                    
                except Exception as e:
                    logger.error(f"Error downloading {shortcode}: {e}")
                    failed_downloads.append(shortcode)
                    continue
            
            # Clean up metadata and non-media files created by instaloader
            for file in os.listdir(download_folder):
                file_path = os.path.join(download_folder, file)
                if os.path.isfile(file_path):
                    # Remove .txt, .json.xz and other metadata files
                    if file.endswith(('.txt', '.json', '.json.xz', '.xz')) or file.startswith('.'):
                        try:
                            os.remove(file_path)
                            logger.info(f"Cleaned up metadata: {file}")
                        except Exception as e:
                            logger.warning(f"Could not remove {file}: {e}")
            
            if not downloaded_files:
                return {'success': False, 'error': 'No posts could be downloaded. Please try again later.'}
            
            # Create ZIP with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_filename = f"{username}_posts_{timestamp}.zip"
            zip_path = os.path.join(self.downloads_folder, zip_filename)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for f in downloaded_files:
                    if os.path.exists(f['path']):
                        zipf.write(f['path'], f['filename'])
                        logger.info(f"Added to ZIP: {f['filename']}")
            
            file_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            
            # Count new vs existing files
            new_downloads = [f for f in downloaded_files if not f.get('already_existed', False)]
            existing_files = [f for f in downloaded_files if f.get('already_existed', False)]
            
            message = f'{len(downloaded_files)} post(s) in ZIP'
            if new_downloads:
                message += f' ({len(new_downloads)} newly downloaded'
                if existing_files:
                    message += f', {len(existing_files)} already existed'
                message += ')'
            if failed_downloads:
                message += f' • {len(failed_downloads)} failed'
            
            return {
                'success': True,
                'filename': zip_filename,
                'size_mb': round(file_size_mb, 2),
                'posts_downloaded': len(downloaded_files),
                'posts_new': len(new_downloads),
                'posts_existing': len(existing_files),
                'posts_failed': len(failed_downloads),
                'message': message,
                'download_folder': download_folder
            }
            
        except Exception as e:
            logger.error(f"Download error: {e}")
            return {'success': False, 'error': f'Download failed: {str(e)}'}

    def _download_from_api(self, url: str) -> Dict[str, Any]:
        """Try downloading from Instagram API"""
        try:
            clean_url = url.split('?')[0].rstrip('/') + '/'
            api_url = clean_url + "?__a=1&__d=dis"
            
            headers = {
                'User-Agent': 'Instagram 318.0.0.0.0 Android',
                'Accept': '*/*',
                'Accept-Language': 'en-US',
            }
            
            response = requests.get(api_url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                media = data.get('items', [{}])[0]
                
                if media.get('video_versions'):
                    return self._save_media(media['video_versions'][0]['url'], '.mp4')
                elif media.get('image_versions2'):
                    return self._save_media(media['image_versions2']['candidates'][0]['url'], '.jpg')
        except Exception as e:
            logger.debug(f"API method failed: {e}")
        
        return {'success': False}

    def _save_media(self, url: str, extension: str) -> Dict[str, Any]:
        """Download and save media from URL with progress tracking"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.instagram.com/',
            }
            
            response = requests.get(url, stream=True, headers=headers, timeout=30)
            
            if response.status_code != 200:
                return {'success': False, 'error': f'HTTP {response.status_code}'}
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            download_id = str(uuid.uuid4())[:8]
            media_type = 'video' if extension == '.mp4' else 'image'
            filename = f"instagram_{media_type}_{timestamp}_{download_id}{extension}"
            filepath = os.path.join(self.downloads_folder, filename)
            
            total_size = 0
            chunk_size = 8192
            max_size = 500 * 1024 * 1024  # 500MB limit
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        total_size += len(chunk)
                        
                        if total_size > max_size:
                            f.close()
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            return {'success': False, 'error': 'File too large (>500MB)'}
            
            # Verify file was downloaded correctly
            if not os.path.exists(filepath):
                return {'success': False, 'error': 'File was not saved correctly'}
            
            file_size = os.path.getsize(filepath)
            if file_size == 0:
                os.remove(filepath)
                return {'success': False, 'error': 'Downloaded file is empty'}
            
            size_mb = file_size / (1024 * 1024)
            
            return {
                'success': True,
                'filepath': filepath,
                'filename': filename,
                'size_mb': round(size_mb, 2),
                'message': f'Downloaded successfully ({round(size_mb, 2)} MB)'
            }
            
        except requests.exceptions.Timeout:
            return {'success': False, 'error': 'Download timeout. Please try again.'}
        except requests.exceptions.ConnectionError:
            return {'success': False, 'error': 'Network connection error. Please check your internet.'}
        except Exception as e:
            logger.error(f"Save media error: {e}")
            return {'success': False, 'error': f'Download failed: {str(e)}'}

    def _download_with_instaloader(self, url: str) -> Dict[str, Any]:
        """Download using Instaloader with improved error handling"""
        username = None
        download_folder = None
        try:
            shortcode = self._extract_shortcode(url)
            if not shortcode:
                return {'success': False, 'error': 'Invalid post URL'}
            
            L = self._create_instaloader(download_media=True)
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            
            # Get username from the post
            username = post.owner_username
            download_folder = os.path.join(self.downloads_folder)
            if not os.path.exists(download_folder):
                print(f"Creating directory: {download_folder}")
            else:
                os.makedirs(download_folder, exist_ok=True)
            
            # Check if this post already exists
            existing_files = [f for f in os.listdir(download_folder) 
                            if shortcode in f and os.path.isfile(os.path.join(download_folder, f))]
            
            if existing_files:
                logger.info(f"Post {shortcode} already exists: {existing_files[0]}")
                existing_path = os.path.join(download_folder, existing_files[0])
                file_size = os.path.getsize(existing_path)
                file_size_mb = file_size / (1024 * 1024)
                
                return {
                    'success': True,
                    'filepath': existing_path,
                    'filename': existing_files[0],
                    'size_mb': round(file_size_mb, 2),
                    'message': f'File already exists ({round(file_size_mb, 2)} MB)',
                    'already_existed': True
                }
            
            # Get files before download
            files_before = set(os.listdir(download_folder))
            
            # Download the post
            L.download_post(post, target=download_folder)
            
            # Wait for download to complete
            time.sleep(2)
            
            # Get files after download
            files_after = set(os.listdir(download_folder))
            new_files = files_after - files_before
            
            if not new_files:
                # Fallback: check all files in folder
                new_files = set(os.listdir(download_folder))
            
            # Find media files
            media_extensions = ('.mp4', '.jpg', '.jpeg', '.png', '.webp')
            files = []
            
            for f in new_files:
                file_path = os.path.join(download_folder, f)
                if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                    file_lower = f.lower()
                    if any(ext in file_lower for ext in media_extensions):
                        files.append(f)
            
            logger.info(f"Found {len(files)} media file(s) for {shortcode}: {files}")
            
            if not files:
                # Log all files for debugging
                all_files = os.listdir(download_folder)
                logger.error(f"No media files found. All files in folder: {all_files}")
                
                # Try to find ANY file with size > 0
                for f in all_files:
                    file_path = os.path.join(download_folder, f)
                    if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                        files.append(f)
                        logger.info(f"Salvaged file: {f}")
                
                if not files:
                    return {'success': False, 'error': 'No media files found after download'}
            
            # Use the first media file
            original_file = os.path.join(download_folder, files[0])
            
            if not os.path.exists(original_file):
                return {'success': False, 'error': 'Downloaded file not found'}
            
            # Determine extension
            file_lower = files[0].lower()
            if '.mp4' in file_lower:
                ext = '.mp4'
            elif '.jpg' in file_lower or '.jpeg' in file_lower:
                ext = '.jpg'
            elif '.png' in file_lower:
                ext = '.png'
            else:
                ext = os.path.splitext(files[0])[1] or '.jpg'
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            media_type = 'video' if ext == '.mp4' else 'image'
            new_filename = f"{username}_{shortcode}{ext}"
            new_filepath = os.path.join(download_folder, new_filename)
            
            # Rename file if needed
            if original_file != new_filepath and not os.path.exists(new_filepath):
                os.rename(original_file, new_filepath)
                logger.info(f"Renamed file from {files[0]} to {new_filename}")
                final_path = new_filepath
            else:
                final_path = original_file
            
            # Clean up metadata files
            for file in os.listdir(download_folder):
                if file.endswith(('.txt', '.json', '.json.xz', '.xz')) or file.startswith('.'):
                    try:
                        os.remove(os.path.join(download_folder, file))
                        logger.info(f"Cleaned up metadata: {file}")
                    except:
                        pass
            
            # Verify file exists and has content
            if not os.path.exists(final_path):
                return {'success': False, 'error': 'File was not saved correctly'}
            
            file_size = os.path.getsize(final_path)
            if file_size == 0:
                os.remove(final_path)
                return {'success': False, 'error': 'Downloaded file is empty'}
            
            file_size_mb = file_size / (1024 * 1024)
            
            return {
                'success': True,
                'filepath': final_path,
                'filename': os.path.basename(final_path),
                'size_mb': round(file_size_mb, 2),
                'message': f'Downloaded successfully ({round(file_size_mb, 2)} MB)',
                'download_folder': download_folder,
                'already_existed': False
            }
            
        except instaloader.exceptions.ConnectionException:
            return {'success': False, 'error': 'Network connection error. Please try again.'}
        except Exception as e:
            logger.error(f"Instaloader download error: {e}")
            return {'success': False, 'error': f'Download failed: {str(e)}'}

    def download_media(self, url: str) -> Dict[str, Any]:
        """Main download method - tries multiple methods"""
        try:
            # Try API method first (fastest)
            result = self._download_from_api(url)
            if result['success']:
                return result
            
            # Fallback to Instaloader
            if self.instaloader_ok:
                return self._download_with_instaloader(url)
            
            return {'success': False, 'error': 'All download methods failed. Please try again.'}
            
        except Exception as e:
            logger.error(f"Download error: {e}")
            return {'success': False, 'error': f'Download failed: {str(e)}'}


# Initialize analyzer
analyzer = InstagramAnalyzer()

# Routes
@app.route('/')
def index():
    return render_template('index.html', instaloader_ok=analyzer.instaloader_ok)

@app.route('/analyze-profile', methods=['POST'])
def analyze_profile():
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Invalid Instagram URL'})
        
        username = analyzer._extract_username(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username'})
        
        return jsonify(analyzer.get_profile_info(username))
            
    except Exception as e:
        logger.error(f"Analyze error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/profile-posts-preview', methods=['POST'])
def profile_posts_preview():
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Invalid Instagram URL'})
        
        username = analyzer._extract_username(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username'})
        
        return jsonify(analyzer.get_profile_posts_preview(username, limit=20))
            
    except Exception as e:
        logger.error(f"Preview error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download-selected-posts', methods=['POST'])
def download_selected_posts():
    try:
        data = request.json
        url = data.get('url', '').strip()
        shortcodes = data.get('selected_posts', [])
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Invalid Instagram URL'})
        
        if not shortcodes:
            return jsonify({'success': False, 'error': 'No posts selected'})
        
        username = analyzer._extract_username(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username'})
        
        return jsonify(analyzer.download_selected_posts(username, shortcodes))
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download', methods=['POST'])
def download():
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Invalid Instagram URL'})
        
        return jsonify(analyzer.download_media(url))
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download-file/<filename>')
def download_file(filename):
    try:
        filepath = os.path.join(analyzer.downloads_folder, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        return send_file(filepath, as_attachment=True, download_name=filename)
    except Exception as e:
        logger.error(f"File download error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/install-engine', methods=['POST'])
def install_engine():
    try:
        success = analyzer.install_instaloader()
        if success:
            return jsonify({'success': True, 'message': 'Engine installed successfully'})
        return jsonify({'success': False, 'error': 'Installation failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)