from flask import Flask, render_template, request, jsonify, send_file
import requests
import re
import os
import threading
from datetime import datetime
import subprocess
import sys
import instaloader
import tempfile
import uuid
from urllib.parse import urlparse
import logging
import json
from typing import Dict, List, Any
import zipfile
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class InstagramAnalyzer:
    def __init__(self):
        self.instaloader_ok = self.check_instaloader()
        self.downloads_folder = "downloads"
        self.setup_directories()

    def setup_directories(self):
        """Create downloads directory if it doesn't exist"""
        if not os.path.exists(self.downloads_folder):
            os.makedirs(self.downloads_folder)

    def check_instaloader(self):
        try:
            import instaloader
            return True
        except ImportError:
            return False

    def install_instaloader(self):
        """Install instaloader package"""
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "instaloader", "--quiet"])
            self.instaloader_ok = True
            return True
        except Exception as e:
            logger.error(f"Installation failed: {e}")
            return False

    def extract_username_from_url(self, url: str) -> str:
        """Extract username from Instagram profile URL"""
        patterns = [
            r'instagram\.com/([A-Za-z0-9_.]+)/?$',
            r'instagram\.com/([A-Za-z0-9_.]+)/?\\?',
            r'instagram\.com/([A-Za-z0-9_.]+)/reels/?',
            r'instagram\.com/([A-Za-z0-9_.]+)/posts/?'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                username = match.group(1)
                # Remove any query parameters
                username = username.split('?')[0]
                return username
        return None

    def is_profile_url(self, url: str) -> bool:
        """Check if URL is a profile URL"""
        profile_patterns = [
            r'instagram\.com/([A-Za-z0-9_.]+)/?$',
            r'instagram\.com/([A-Za-z0-9_.]+)/?\\?',
            r'instagram\.com/([A-Za-z0-9_.]+)/?$',
            r'instagram\.com/([A-Za-z0-9_.]+)/?\\?'
        ]
        
        for pattern in profile_patterns:
            if re.search(pattern, url):
                return True
        return False

    def is_post_url(self, url: str) -> bool:
        """Check if URL is a post/reel URL"""
        post_patterns = [
            r'instagram\.com/p/([A-Za-z0-9_-]+)',
            r'instagram\.com/reel/([A-Za-z0-9_-]+)',
            r'instagram\.com/stories/[^/]+/([A-Za-z0-9_-]+)',
            r'instagram\.com/tv/([A-Za-z0-9_-]+)'
        ]
        
        for pattern in post_patterns:
            if re.search(pattern, url):
                return True
        return False

    def get_profile_posts_preview(self, username: str, limit: int = 20) -> Dict[str, Any]:
        """Get profile posts with media URLs for preview and download"""
        try:
            if not self.instaloader_ok:
                return {'success': False, 'error': 'Instaloader not available'}
            
            L = instaloader.Instaloader(
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True
            )
            
            profile = instaloader.Profile.from_username(L.context, username)
            
            posts = []
            post_count = 0
            
            for post in profile.get_posts():
                if post_count >= limit:
                    break
                
                try:
                    # Get actual media URLs for each post
                    media_urls = []
                    thumbnail_url = None
                    
                    if post.is_video:
                        # For videos, get the actual video URL
                        if hasattr(post, 'video_url') and post.video_url:
                            media_urls.append({
                                'url': post.video_url,
                                'type': 'video',
                                'extension': '.mp4'
                            })
                        # Get thumbnail for video
                        thumbnail_url = post.url
                    else:
                        # For images, get the actual image URL
                        if hasattr(post, 'url') and post.url:
                            media_urls.append({
                                'url': post.url,
                                'type': 'image', 
                                'extension': '.jpg'
                            })
                            thumbnail_url = post.url
                    
                    # If no media URLs found, try to get from sidecar
                    if not media_urls and post.mediacount > 1:
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
                    
                    # Calculate engagement rate
                    engagement_rate = self.calculate_engagement_rate(post, profile.followers)
                    
                    post_data = {
                        'shortcode': post.shortcode,
                        'url': f"https://instagram.com/p/{post.shortcode}",
                        'caption': post.caption if post.caption else '',
                        'likes': post.likes,
                        'comments': post.comments,
                        'timestamp': post.date_utc.isoformat(),
                        'is_video': post.is_video,
                        'video_view_count': post.video_view_count if post.is_video else 0,
                        'engagement_rate': engagement_rate,
                        'media_count': post.mediacount,
                        'type': 'video' if post.is_video else 'carousel' if post.mediacount > 1 else 'image',
                        'media_urls': media_urls,
                        'thumbnail_url': thumbnail_url or 'https://via.placeholder.com/300?text=No+Preview'
                    }
                    posts.append(post_data)
                    post_count += 1
                    
                except Exception as e:
                    logger.error(f"Error processing post {post.shortcode}: {e}")
                    continue
            
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
            
        except instaloader.exceptions.ProfileNotExistsException:
            return {'success': False, 'error': f'Profile @{username} does not exist'}
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            return {'success': False, 'error': f'Profile @{username} is private'}
        except Exception as e:
            logger.error(f"Profile posts preview error: {e}")
            return {'success': False, 'error': f'Failed to fetch profile posts: {str(e)}'}

    def find_downloaded_media(self, folder_path, shortcode, username):
        """Find downloaded media file in folder"""
        try:
            for file in os.listdir(folder_path):
                if shortcode in file and not file.endswith('.json') and not file.endswith('.txt'):
                    return os.path.join(folder_path, file)
            return None
        except Exception as e:
            logger.error(f"Error finding media: {e}")
            return None

    def download_selected_posts(self, username: str, post_shortcodes: List[str]) -> Dict[str, Any]:
        """Download selected posts from a profile"""
        try:
            if not self.instaloader_ok:
                return {'success': False, 'error': 'Instaloader not available'}
            
            L = instaloader.Instaloader(
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                post_metadata_txt_pattern="",
                filename_pattern="{profile}_{shortcode}",
                quiet=True
            )
            
            profile = instaloader.Profile.from_username(L.context, username)
            
            # Create download folder with consistent naming
            download_id = str(uuid.uuid4())[:8]
            profile_folder = os.path.join(self.downloads_folder, f"{username}_{download_id}")
            os.makedirs(profile_folder, exist_ok=True)
            
            downloaded_files = []
            successful_downloads = 0
            
            # نیا کوڈ: ہر پوسٹ کو ڈاؤن لوڈ کریں
            for shortcode in post_shortcodes:
                try:
                    post = instaloader.Post.from_shortcode(L.context, shortcode)
                    L.download_post(post, target=profile_folder)

                    # ڈاؤن لوڈ شدہ فائل کو تلاش کریں
                    downloaded_file = self.find_downloaded_media(profile_folder, shortcode, username)
                    
                    if downloaded_file and os.path.exists(downloaded_file):
                        filename = os.path.basename(downloaded_file)
                        ext = os.path.splitext(filename)[1]
                        new_name = f"{username}_{shortcode}{ext}"
                        new_path = os.path.join(self.downloads_folder, new_name)
                        
                        # Rename + move
                        os.rename(downloaded_file, new_path)
                        downloaded_files.append({
                            'filename': new_name,
                            'path': new_path,
                            'shortcode': shortcode
                        })
                        successful_downloads += 1
                        logger.info(f"موفقیت: {new_name}")
                    else:
                        logger.warning(f"فائل نہیں ملی: {shortcode}")

                except Exception as e:
                    logger.error(f"ڈاؤن لوڈ ایریر: {e}")
                    continue

            # آخر میں ZIP بنائیں
            if downloaded_files:
                zip_filename = f"{username}_selected_{download_id}.zip"
                zip_path = os.path.join(self.downloads_folder, zip_filename)
                
                with zipfile.ZipFile(zip_path, 'w') as zipf:
                    for f in downloaded_files:
                        zipf.write(f['path'], f['filename'])
                
                # فائلیں ڈیلیٹ کریں
                for f in downloaded_files:
                    try: 
                        os.remove(f['path'])
                    except: 
                        pass
                
                return {
                    'success': True,
                    'filename': zip_filename,
                    'size_mb': os.path.getsize(zip_path) // (1024*1024),
                    'posts_downloaded': successful_downloads,
                    'files_downloaded': len(downloaded_files),
                    'message': f'{successful_downloads} پوسٹس کامیابی سے ڈاؤن لوڈ'
                }
            else:
                # Log detailed error information
                logger.error(f"No files could be downloaded. Folder contents: {os.listdir(profile_folder) if os.path.exists(profile_folder) else 'Folder not found'}")
                return {'success': False, 'error': 'No posts could be downloaded - check if posts exist and are accessible'}
                
        except Exception as e:
            logger.error(f"Selected posts download error: {e}")
            return {'success': False, 'error': f'Failed to download selected posts: {str(e)}'}

    def get_profile_info(self, username: str) -> Dict[str, Any]:
        """Get comprehensive profile information"""
        try:
            if not self.instaloader_ok:
                return {'success': False, 'error': 'Instaloader not available'}
            
            L = instaloader.Instaloader(
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True
            )
            
            profile = instaloader.Profile.from_username(L.context, username)
            
            # Get latest posts (up to 12)
            posts = []
            post_count = 0
            for post in profile.get_posts():
                if post_count >= 12:
                    break
                
                engagement_rate = self.calculate_engagement_rate(post, profile.followers)
                
                post_data = {
                    'shortcode': post.shortcode,
                    'url': f"https://instagram.com/p/{post.shortcode}",
                    'caption': post.caption if post.caption else '',
                    'likes': post.likes,
                    'comments': post.comments,
                    'timestamp': post.date_utc.isoformat(),
                    'is_video': post.is_video,
                    'video_view_count': post.video_view_count if post.is_video else 0,
                    'engagement_rate': engagement_rate,
                    'media_count': post.mediacount,
                    'type': 'video' if post.is_video else 'carousel' if post.mediacount > 1 else 'image',
                    'thumbnail_url': post.url if hasattr(post, 'url') else 'https://via.placeholder.com/300?text=No+Preview'
                }
                posts.append(post_data)
                post_count += 1
            
            analytics = self.calculate_profile_analytics(profile, posts)
            
            profile_info = {
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
                    'total_igtv_videos': getattr(profile, 'igtv_video_count', 0)
                },
                'posts': posts,
                'analytics': analytics
            }
            
            return profile_info
            
        except Exception as e:
            logger.error(f"Profile info error: {e}")
            return {'success': False, 'error': f'Failed to fetch profile: {str(e)}'}

    def calculate_engagement_rate(self, post, followers: int) -> float:
        """Calculate engagement rate for a single post"""
        if followers == 0:
            return 0.0
        
        total_engagement = post.likes + post.comments
        engagement_rate = (total_engagement / followers) * 100
        return round(engagement_rate, 2)

    def calculate_profile_analytics(self, profile, posts: List[Dict]) -> Dict[str, Any]:
        """Calculate comprehensive profile analytics"""
        if not posts:
            return {}
        
        total_likes = sum(post['likes'] for post in posts)
        total_comments = sum(post['comments'] for post in posts)
        total_engagement = total_likes + total_comments
        
        avg_engagement_rate = sum(post['engagement_rate'] for post in posts) / len(posts)
        
        video_posts = [post for post in posts if post['is_video']]
        image_posts = [post for post in posts if not post['is_video']]
        
        video_engagement = sum(post['likes'] + post['comments'] for post in video_posts)
        image_engagement = sum(post['likes'] + post['comments'] for post in image_posts)
        
        analytics = {
            'total_posts_analyzed': len(posts),
            'average_likes_per_post': round(total_likes / len(posts), 1),
            'average_comments_per_post': round(total_comments / len(posts), 1),
            'average_engagement_rate': round(avg_engagement_rate, 2),
            'total_engagement': total_engagement,
            'video_posts_count': len(video_posts),
            'image_posts_count': len(image_posts),
            'video_engagement': video_engagement,
            'image_engagement': image_engagement,
            'engagement_per_follower': round((total_engagement / profile.followers) * 100, 4) if profile.followers > 0 else 0
        }
        
        return analytics

    def ultra_fast_download(self, url):
        """ULTRA FAST DOWNLOAD - 2025 WORKING METHOD"""
        try:
            logger.info("ULTRA FAST Download شروع ہو رہا ہے...")

            # Clean URL
            clean_url = url.split('?')[0].rstrip('/') + '/'

            # METHOD 1: New Instagram ?__a=1&__d=dis (2025 working)
            try:
                api_url = clean_url + "?__a=1&__d=dis"
                headers = {
                    'User-Agent': 'Instagram 318.0.0.0.0 Android',
                    'Accept': '*/*',
                    'Accept-Encoding': 'gzip, deflate',
                    'Accept-Language': 'en-US',
                    'Connection': 'keep-alive',
                    'Range': 'bytes=0-',  # یہ ویڈیو ڈاؤن لوڈ کے لیے ضروری ہے
                }
                r = requests.get(api_url, headers=headers, timeout=15)
                
                if r.status_code == 200:
                    data = r.json()
                    media = data.get('items', [{}])[0]
                    
                    if media.get('video_versions'):
                        video_url = media['video_versions'][0]['url']
                        return self.save_media_from_url(video_url, '.mp4')
                    elif media.get('image_versions2'):
                        img_url = media['image_versions2']['candidates'][0]['url']
                        return self.save_media_from_url(img_url, '.jpg')
            except: 
                pass

            # METHOD 2: ddlgram.com API (100% working in 2025)
            try:
                ddl_api = f"https://api.ddlgram.com/v2/download?url={url}"
                r = requests.get(ddl_api, timeout=15)
                if r.status_code == 200:
                    j = r.json()
                    if j.get('success') and j.get('media'):
                        media_url = j['media'][0]['url']
                        ext = '.mp4' if 'video' in j['media'][0]['type'] else '.jpg'
                        return self.save_media_from_url(media_url, ext)
            except: 
                pass

            # METHOD 3: Instaloader fallback (already working)
            if self.instaloader_ok:
                return self.instaloader_download(url)

            return {'success': False, 'error': 'سب میتھڈز فیل ہو گئے'}

        except Exception as e:
            logger.error(f"Ultra fast error: {e}")
            return {'success': False, 'error': str(e)}

    def download_media(self, url):
        """Main download method for posts, reels, and profiles"""
        try:
            logger.info(f"Starting download for URL: {url}")
            
            if self.is_profile_url(url):
                return self.download_profile_posts(url, limit=10)
            
            result = self.ultra_fast_download(url)
            if result['success']:
                return result

            return {'success': False, 'error': 'Failed to download media from all methods'}

        except Exception as e:
            logger.error(f"Download error: {e}")
            return {'success': False, 'error': str(e)}

    def save_media_from_url(self, media_url, file_extension):
        """Save media from direct URL"""
        try:
            logger.info(f"Downloading from URL: {media_url[:100]}...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'identity',  # Changed to identity to avoid encoding issues
                'Referer': 'https://www.instagram.com/',
                'Origin': 'https://www.instagram.com',
                'Connection': 'keep-alive',
            }
            
            response = requests.get(media_url, stream=True, headers=headers, timeout=30)
            logger.info(f"Download response status: {response.status_code}")
            
            if response.status_code == 200:
                download_id = str(uuid.uuid4())[:8]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                if file_extension == '.mp4':
                    filename = f"instagram_video_{timestamp}_{download_id}.mp4"
                else:
                    filename = f"instagram_image_{timestamp}_{download_id}.jpg"
                    
                filepath = os.path.join(self.downloads_folder, filename)
                
                total_size = 0
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            total_size += len(chunk)
                            if total_size > 500 * 1024 * 1024:  # 500MB limit
                                raise Exception("File too large")
                
                file_size_mb = total_size // (1024 * 1024)
                
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    logger.info(f"Direct download successful: {filename} ({file_size_mb} MB)")
                    
                    return {
                        'success': True,
                        'filepath': filepath,
                        'filename': filename,
                        'size_mb': file_size_mb,
                        'message': f'Downloaded {file_size_mb} MB successfully!'
                    }
                else:
                    return {'success': False, 'error': 'Downloaded file is empty or missing'}
            else:
                logger.error(f"HTTP error {response.status_code} for URL: {media_url}")
                return {'success': False, 'error': f'HTTP error {response.status_code}'}

        except Exception as e:
            logger.error(f"URL download error: {e}")
            return {'success': False, 'error': f'Direct download failed: {str(e)}'}

    def instaloader_download(self, url):
        """Download using instaloader"""
        try:
            L = instaloader.Instaloader(
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                post_metadata_txt_pattern="",
                filename_pattern="{shortcode}",
                quiet=True
            )

            code = (re.search(r'/reel/([A-Za-z0-9_-]+)', url) or 
                   re.search(r'/p/([A-Za-z0-9_-]+)', url) or
                   re.search(r'/reel/([A-Za-z0-9_-]+)', url))
            
            if not code:
                return {'success': False, 'error': 'Could not extract post ID from URL'}

            shortcode = code.group(1)
            logger.info(f"Downloading post with shortcode: {shortcode}")
            
            post = instaloader.Post.from_shortcode(L.context, shortcode)

            download_id = str(uuid.uuid4())[:8]
            temp_folder = os.path.join(self.downloads_folder, download_id)
            os.makedirs(temp_folder, exist_ok=True)

            L.download_post(post, target=temp_folder)

            files = [f for f in os.listdir(temp_folder) 
                    if f.endswith(('.mp4', '.jpg', '.png'))]

            logger.info(f"Found files: {files}")

            if files:
                original_file = os.path.join(temp_folder, files[0])
                
                file_ext = os.path.splitext(original_file)[1]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                if file_ext == '.mp4':
                    new_filename = f"instagram_video_{timestamp}_{shortcode}.mp4"
                else:
                    new_filename = f"instagram_image_{timestamp}_{shortcode}.jpg"
                    
                new_filepath = os.path.join(self.downloads_folder, new_filename)
                
                # Move file to main downloads folder
                if os.path.exists(original_file):
                    os.rename(original_file, new_filepath)
                
                # Clean up temp folder
                import shutil
                if os.path.exists(temp_folder):
                    shutil.rmtree(temp_folder)
                
                if os.path.exists(new_filepath):
                    file_size = os.path.getsize(new_filepath) // (1024 * 1024)
                    
                    logger.info(f"Download successful: {new_filename} ({file_size} MB)")
                    
                    return {
                        'success': True,
                        'filepath': new_filepath,
                        'filename': new_filename,
                        'size_mb': file_size,
                        'message': f'Downloaded {file_size} MB successfully!'
                    }
                else:
                    return {'success': False, 'error': 'File move failed'}
            else:
                # Clean up temp folder
                import shutil
                if os.path.exists(temp_folder):
                    shutil.rmtree(temp_folder)
                return {'success': False, 'error': 'No media files found after download'}

        except Exception as e:
            logger.error(f"Instaloader error: {e}")
            return {'success': False, 'error': f'Instaloader failed: {str(e)}'}

    def download_profile_posts(self, url: str, limit: int = 10) -> Dict[str, Any]:
        """Download multiple posts from a profile"""
        try:
            if not self.instaloader_ok:
                return {'success': False, 'error': 'Instaloader not available for profile downloads'}
            
            username = self.extract_username_from_url(url)
            if not username:
                return {'success': False, 'error': 'Could not extract username from URL'}
            
            return self.download_selected_posts(username, [])
                
        except Exception as e:
            logger.error(f"Profile download error: {e}")
            return {'success': False, 'error': f'Failed to download profile: {str(e)}'}

# Global instance
analyzer = InstagramAnalyzer()

@app.route('/')
def index():
    return render_template('index.html', instaloader_ok=analyzer.instaloader_ok)

@app.route('/analyze-profile', methods=['POST'])
def analyze_profile():
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Please enter a valid Instagram URL'})
        
        username = analyzer.extract_username_from_url(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username from URL'})
        
        result = analyzer.get_profile_info(username)
        return jsonify(result)
            
    except Exception as e:
        logger.error(f"Profile analysis error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/profile-posts-preview', methods=['POST'])
def profile_posts_preview():
    """Get profile posts for preview and selection"""
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Please enter a valid Instagram URL'})
        
        username = analyzer.extract_username_from_url(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username from URL'})
        
        result = analyzer.get_profile_posts_preview(username, limit=20)
        return jsonify(result)
            
    except Exception as e:
        logger.error(f"Profile posts preview error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download-selected-posts', methods=['POST'])
def download_selected_posts():
    """Download selected posts from profile"""
    try:
        data = request.json
        url = data.get('url', '').strip()
        post_shortcodes = data.get('selected_posts', [])
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Please enter a valid Instagram URL'})
        
        if not post_shortcodes:
            return jsonify({'success': False, 'error': 'No posts selected for download'})
        
        username = analyzer.extract_username_from_url(url)
        if not username:
            return jsonify({'success': False, 'error': 'Could not extract username from URL'})
        
        result = analyzer.download_selected_posts(username, post_shortcodes)
        return jsonify(result)
            
    except Exception as e:
        logger.error(f"Selected posts download error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download', methods=['POST'])
def download():
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Please enter a valid Instagram URL'})
        
        result = analyzer.download_media(url)
        
        if result['success']:
            response_data = {
                'success': True,
                'filename': result['filename'],
                'message': result['message'],
                'size_mb': result['size_mb']
            }
            if 'type' in result:
                response_data['type'] = result['type']
            if 'posts_downloaded' in result:
                response_data['posts_downloaded'] = result['posts_downloaded']
                
            return jsonify(response_data)
        else:
            return jsonify({'success': False, 'error': result['error']})
            
    except Exception as e:
        logger.error(f"Download route error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download-file/<filename>')
def download_file(filename):
    """Serve downloaded file"""
    try:
        filepath = os.path.join(analyzer.downloads_folder, filename)
        
        if os.path.exists(filepath):
            return send_file(filepath, as_attachment=True)
        else:
            return jsonify({'success': False, 'error': 'File not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/install-engine', methods=['POST'])
def install_engine():
    """Install instaloader"""
    try:
        success = analyzer.install_instaloader()
        if success:
            return jsonify({'success': True, 'message': 'Engine installed successfully!'})
        else:
            return jsonify({'success': False, 'error': 'Installation failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/debug-downloads')
def debug_downloads():
    """Debug endpoint to check downloads folder"""
    try:
        downloads_path = analyzer.downloads_folder
        if not os.path.exists(downloads_path):
            return jsonify({'error': 'Downloads folder does not exist'})
        
        all_items = []
        for item in os.listdir(downloads_path):
            item_path = os.path.join(downloads_path, item)
            item_info = {
                'name': item,
                'is_file': os.path.isfile(item_path),
                'is_dir': os.path.isdir(item_path),
                'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0
            }
            all_items.append(item_info)
        
        return jsonify({
            'downloads_folder': downloads_path,
            'exists': os.path.exists(downloads_path),
            'items': all_items
        })
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/ultra-fast-download', methods=['POST'])
def ultra_fast_download_route():
    """Direct ultra fast download endpoint"""
    try:
        url = request.json.get('url', '').strip()
        
        if not url or "instagram.com" not in url:
            return jsonify({'success': False, 'error': 'Please enter a valid Instagram URL'})
        
        result = analyzer.ultra_fast_download(url)
        
        if result['success']:
            response_data = {
                'success': True,
                'filename': result['filename'],
                'message': result['message'],
                'size_mb': result['size_mb'],
                'method': 'ultra_fast'
            }
            if 'type' in result:
                response_data['type'] = result['type']
            if 'posts_downloaded' in result:
                response_data['posts_downloaded'] = result['posts_downloaded']
                
            return jsonify(response_data)
        else:
            return jsonify({'success': False, 'error': result['error']})
            
    except Exception as e:
        logger.error(f"Ultra fast download route error: {e}")
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)