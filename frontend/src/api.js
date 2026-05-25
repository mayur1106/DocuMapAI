const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    const message = typeof payload === "object" && payload?.detail ? payload.detail : "Request failed";
    throw new Error(message);
  }

  return payload;
}

export function listDocuments(workflow) {
  const suffix = workflow ? `?workflow=${encodeURIComponent(workflow)}` : "";
  return request(`/documents${suffix}`);
}

export function getActivityLogs(limit = 30) {
  return request(`/activity?limit=${encodeURIComponent(limit)}`);
}

export function deleteDocument(documentId) {
  return request(`/documents/${documentId}`, { method: "DELETE" });
}

export function convertXml(documentId) {
  return request(`/convert-xml/${documentId}`, { method: "POST" });
}

export function getXmlStats(documentId) {
  return request(`/xml/${documentId}/stats`);
}

export function getXmlPreview(documentId) {
  return request(`/xml/${documentId}/preview`);
}

export function uploadDocument(file, onProgress, workflow = "toc") {
  const formData = new FormData();
  formData.append("file", file);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE_URL}/upload?workflow=${encodeURIComponent(workflow)}`);

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || !onProgress) return;
      onProgress(Math.round((event.loaded / event.total) * 100));
    };

    xhr.onload = () => {
      try {
        const payload = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
        } else {
          reject(new Error(payload.detail || "Upload failed"));
        }
      } catch (error) {
        reject(error);
      }
    };

    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(formData);
  });
}

export function generateToc(documentId) {
  return request(`/generate-toc/${documentId}`, { method: "POST" });
}

export function repairExistingToc(documentId) {
  return request(`/hyperlink-existing-toc/${documentId}`, { method: "POST" });
}

export function getJobStatus(jobId) {
  return request(`/status/${jobId}`);
}

export function getToc(documentId) {
  return request(`/toc/${documentId}`);
}

export function getProcessStream(documentId) {
  return request(`/process-stream/${documentId}`);
}

export function getDownloadUrl(documentId) {
  return `${API_BASE_URL}/download/${documentId}`;
}

export function getUnresolvedReportDownloadUrl(documentId) {
  return `${API_BASE_URL}/unresolved-report/${documentId}/download`;
}

export function getXmlDownloadUrl(documentId) {
  return `${API_BASE_URL}/xml/${documentId}/download`;
}

export { API_BASE_URL };
