from .supabase_client import create_client, Client
from app.core import settings
import uuid

class SupabaseStorageClient:

    def __init__(self, bucket_name: str = "Documents"):
        self.bucket_name  = bucket_name

    def _get_client(self) -> Client:
        return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

    async def upload_docs(
        self, 
        file_bytes: bytes,
        user_id: uuid.UUID,
        folder: str,
        file_name: str | None = None,
        content_type: str = "pdf"
    ) -> str:

        if not file_name:
            extension = content_type.split("/")[-1]
            file_name = f"{uuid.uuid4}.{extension}"
        
        file_path = f"{user_id}/{folder}/{file_name}"

        client = self._get_client()
        client.storage.from_(self.bucket_name).upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": content_type}
        )

        url_response = client.storage.from_(self.bucket_name).get_public_url(file_path)

        return url_response
    
    async def delete_docs(self, folder: str, file_name: str, user_id: uuid.UUID) -> bool:
        try:
            client = self._get_client()

            file_path = f"{user_id}/{folder}/{file_name}"

            client.storage.from_(self.bucket_name).remove(file_path)
            return True
        except Exception:
            return False
    
def get_storage_client() -> SupabaseStorageClient:
    return SupabaseStorageClient()