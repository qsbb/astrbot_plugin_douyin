"""所有网页 UI 定位集中管理；定位不唯一时停止，不尝试随意的首个元素。"""

VIDEO = "video"
COMMENT_EDITOR = '[data-e2e="comment-input"] [contenteditable="true"], [data-e2e="comment-input"][contenteditable="true"], [contenteditable="true"][data-placeholder*="评论"], textarea[placeholder*="评论"]'
COMMENT_SUBMIT = '[data-e2e="comment-post"], button[aria-label="发布评论"]'
LIKE = '[data-e2e="video-player-digg"], button[aria-label="点赞"], button[aria-label="取消点赞"]'
COMMENT_OPEN = '[data-e2e="video-player-comment"], button[aria-label="评论"]'
SHARE_OPEN = '[data-e2e="video-player-share"], button[aria-label="分享"]'
SHARE_DIALOG = '[role="dialog"], [data-e2e="share-panel"]'
MESSAGES_OPEN = '[data-e2e="im-entry"], button[aria-label="私信"], a[href*="/im"]'
MESSAGE_EDITOR = '[data-e2e="im-input"] [contenteditable="true"], [data-e2e="im-input"][contenteditable="true"], [contenteditable="true"][data-placeholder*="消息"]'
MESSAGE_SEND = '[data-e2e="im-send"], button[aria-label="发送消息"]'
INBOX_CONVERSATIONS = "[data-conversation-id]"
INBOX_MESSAGES = "[data-message-id]"
