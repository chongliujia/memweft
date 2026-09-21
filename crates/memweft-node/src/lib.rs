use napi_derive::napi;
use std::sync::Mutex;

#[napi]
pub struct NativeMemory {
    inner: Mutex<Option<memweft::Memory>>,
}

#[napi]
impl NativeMemory {
    #[napi(factory)]
    pub async fn open(path: String) -> napi::Result<Self> {
        tokio::task::spawn_blocking(move || {
            memweft::Memory::open(path)
                .map(|memory| Self {
                    inner: Mutex::new(Some(memory)),
                })
                .map_err(|e| napi::Error::from_reason(e.to_string()))
        })
        .await
        .map_err(|e| napi::Error::from_reason(e.to_string()))?
    }

    #[napi]
    pub fn close(&self) -> napi::Result<()> {
        self.inner
            .lock()
            .map_err(|_| napi::Error::from_reason("Memory lock poisoned"))?
            .take();
        Ok(())
    }

    #[napi]
    pub async fn request(&self, request: String) -> napi::Result<String> {
        let memory = self
            .inner
            .lock()
            .map_err(|_| napi::Error::from_reason("Memory lock poisoned"))?
            .as_ref()
            .cloned()
            .ok_or_else(|| napi::Error::from_reason("Memory is closed"))?;
        tokio::task::spawn_blocking(move || {
            let value = serde_json::from_str(&request)
                .map_err(|e| napi::Error::from_reason(e.to_string()))?;
            let result = memory
                .request(value)
                .map_err(|e| napi::Error::from_reason(e.to_string()))?;
            serde_json::to_string(&result).map_err(|e| napi::Error::from_reason(e.to_string()))
        })
        .await
        .map_err(|e| napi::Error::from_reason(e.to_string()))?
    }
}
