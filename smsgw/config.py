from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # voip.ms API (enable it + whitelist your public IP in the portal)
    voipms_api_username: str  # your voip.ms login e-mail
    voipms_api_password: str  # the separate API password, not the portal password
    voipms_did: str  # 10-digit SMS-enabled DID, e.g. 5145551234
    voipms_api_url: str = "https://voip.ms/api/v1/rest.php"

    # Telegram
    telegram_bot_token: str
    # Forum supergroup id (-100...). Leave 0 to run in discovery mode: the bot replies
    # to any message with that chat's id so you can fill this in.
    telegram_chat_id: int = 0
    # Comma-separated Telegram user ids allowed to send SMS. Empty = every group member.
    allowed_user_ids: str = ""

    # Webhook (voip.ms URL callback), served behind Caddy
    webhook_secret: str = Field(min_length=16)
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080

    db_path: str = "smsgw.db"
    daily_send_limit: int = 100
    reconcile_interval: int = 300  # seconds
    # How to send text longer than 160 chars: one MMS (up to 2048 chars) or several SMS.
    long_text_mode: Literal["mms", "split"] = "mms"
    # Per image before base64 (+37%); voip.ms rejects base64 media over ~1.2 MB and carriers prefer < 1 MB.
    mms_max_image_bytes: int = 600_000
    log_level: str = "INFO"

    @property
    def allowed_users(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.replace(" ", "").split(",") if x}
