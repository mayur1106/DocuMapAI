import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowUpDown,
  Bell,
  BookOpen,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Database,
  FileCheck2,
  FileCog2,
  FileText,
  FolderOpen,
  HardDrive,
  Layers3,
  Link2,
  ListFilter,
  Loader2,
  Play,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  Sparkles,
  Trash2,
  UploadCloud,
  WandSparkles,
  XCircle,
} from "lucide-react";
import {
  API_BASE_URL,
  convertXml,
  deleteDocument,
  generateToc,
  getActivityLogs,
  getDownloadUrl,
  getJobStatus,
  getProcessStream,
  getToc,
  getUnresolvedReportDownloadUrl,
  getXmlDownloadUrl,
  getXmlPreview,
  getXmlStats,
  listDocuments,
  repairExistingToc,
  uploadDocument,
} from "./api";

const POLL_INTERVAL_MS = 2500;

const NAV_ITEMS = [
  { id: "toc", label: "TOC & Hyperlinking", icon: Activity },
  { id: "xml", label: "PDF to XML", icon: Database },
  { id: "activity", label: "Activity Logs", icon: ClipboardList },
];

const WORKFLOWS = {
  TOC: "toc",
  XML: "xml",
};

function App() {
  const [documents, setDocuments] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [activeView, setActiveView] = useState("toc");
  const [tocEntries, setTocEntries] = useState([]);
  const [tocSearch, setTocSearch] = useState("");
  const [xmlStats, setXmlStats] = useState(null);
  const [xmlPreview, setXmlPreview] = useState("");
  const [xmlConverting, setXmlConverting] = useState(false);
  const [xmlConvertingId, setXmlConvertingId] = useState("");
  const [xmlDetailsLoading, setXmlDetailsLoading] = useState(false);
  const [xmlSearch, setXmlSearch] = useState("");
  const [xmlStatusFilter, setXmlStatusFilter] = useState("all");
  const [xmlSort, setXmlSort] = useState({ key: "updated_at", direction: "desc" });
  const [xmlPage, setXmlPage] = useState(1);
  const [xmlPageSize, setXmlPageSize] = useState(8);
  const [documentSearch, setDocumentSearch] = useState("");
  const [documentStatusFilter, setDocumentStatusFilter] = useState("all");
  const [documentSort, setDocumentSort] = useState({ key: "updated_at", direction: "desc" });
  const [documentPage, setDocumentPage] = useState(1);
  const [documentPageSize, setDocumentPageSize] = useState(8);
  const [tocSort, setTocSort] = useState({ key: "page", direction: "asc" });
  const [tocPage, setTocPage] = useState(1);
  const [tocPageSize, setTocPageSize] = useState(25);
  const [activeJob, setActiveJob] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [activityLogs, setActivityLogs] = useState([]);
  const [processStream, setProcessStream] = useState("");
  const [activityLoading, setActivityLoading] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [isDragging, setIsDragging] = useState(false);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState("");
  const [notice, setNotice] = useState(null);
  const fileInputRef = useRef(null);

  const tocDocuments = useMemo(() => documents.filter((document) => workflowForDocument(document) === WORKFLOWS.TOC), [documents]);
  const xmlDocuments = useMemo(() => documents.filter((document) => workflowForDocument(document) === WORKFLOWS.XML), [documents]);
  const isActivityView = activeView === "activity";
  const activeDocuments = isActivityView ? documents : activeView === WORKFLOWS.XML ? xmlDocuments : tocDocuments;

  const selectedDocument = useMemo(
    () => activeDocuments.find((document) => document.id === selectedId) || activeDocuments[0] || null,
    [activeDocuments, selectedId]
  );

  const documentTable = useMemo(() => {
    const query = documentSearch.trim().toLowerCase();
    const filtered = tocDocuments.filter((document) => {
      const normalizedStatus = lifecycleStatus(document.status);
      const searchable = [
        document.filename,
        document.id,
        normalizedStatus,
        document.created_by,
        document.updated_by,
        document.has_xml ? "xml" : "",
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      const matchesSearch = !query || searchable.includes(query);
      const matchesStatus = documentStatusFilter === "all" || normalizedStatus === documentStatusFilter;
      return matchesSearch && matchesStatus;
    });
    const sorted = sortItems(filtered, documentSort);
    const totalPages = Math.max(1, Math.ceil(sorted.length / documentPageSize));
    const page = Math.min(documentPage, totalPages);
    const startIndex = (page - 1) * documentPageSize;

    return {
      filteredCount: sorted.length,
      page,
      rows: sorted.slice(startIndex, startIndex + documentPageSize),
      startIndex,
      totalPages,
    };
  }, [tocDocuments, documentPage, documentPageSize, documentSearch, documentSort, documentStatusFilter]);

  const tocTable = useMemo(() => {
    const query = tocSearch.trim().toLowerCase();
    const filtered = tocEntries.filter((entry) =>
      !query ? true : `${entry.title} ${entry.page} ${entry.level}`.toLowerCase().includes(query)
    );
    const sorted = sortItems(filtered, tocSort);
    const totalPages = Math.max(1, Math.ceil(sorted.length / tocPageSize));
    const page = Math.min(tocPage, totalPages);
    const startIndex = (page - 1) * tocPageSize;

    return {
      filteredCount: sorted.length,
      page,
      rows: sorted.slice(startIndex, startIndex + tocPageSize),
      startIndex,
      totalPages,
    };
  }, [tocEntries, tocPage, tocPageSize, tocSearch, tocSort]);

  const xmlDocumentTable = useMemo(() => {
    const query = xmlSearch.trim().toLowerCase();
    const enriched = xmlDocuments.map((document) => ({
      ...document,
      xml_status: xmlStatusForDocument(document, xmlConvertingId),
    }));
    const filtered = enriched.filter((document) => {
      const searchable = [
        document.filename,
        document.id,
        document.xml_status,
        document.created_by,
        document.updated_by,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      const matchesSearch = !query || searchable.includes(query);
      const matchesStatus = xmlStatusFilter === "all" || document.xml_status === xmlStatusFilter;
      return matchesSearch && matchesStatus;
    });
    const sorted = sortItems(filtered, xmlSort);
    const totalPages = Math.max(1, Math.ceil(sorted.length / xmlPageSize));
    const page = Math.min(xmlPage, totalPages);
    const startIndex = (page - 1) * xmlPageSize;

    return {
      filteredCount: sorted.length,
      page,
      rows: sorted.slice(startIndex, startIndex + xmlPageSize),
      startIndex,
      totalPages,
    };
  }, [xmlDocuments, xmlConvertingId, xmlPage, xmlPageSize, xmlSearch, xmlSort, xmlStatusFilter]);

  const metrics = useMemo(() => {
    const completed = activeDocuments.filter((document) => isReadyStatus(document.status)).length;
    const failed = activeDocuments.filter((document) => document.status === "failed").length;
    const processingCount = activeDocuments.filter((document) => ["queued", "processing"].includes(lifecycleStatus(document.status))).length;
    return {
      total: activeDocuments.length,
      completed,
      failed,
      processing: processingCount,
      tocCount: tocEntries.length,
      xmlCount: xmlDocuments.filter((document) => document.has_xml).length,
    };
  }, [activeDocuments, tocEntries, xmlDocuments]);

  useEffect(() => {
    refreshDocuments();
    loadActivityLogs();
  }, []);

  useEffect(() => {
    if (!activeDocuments.length) {
      if (selectedId) setSelectedId("");
      return;
    }
    if (!selectedId || !activeDocuments.some((document) => document.id === selectedId)) {
      setSelectedId(activeDocuments[0].id);
    }
  }, [activeDocuments, selectedId]);

  useEffect(() => {
    setDocumentPage(1);
  }, [documentPageSize, documentSearch, documentStatusFilter, documentSort.key, documentSort.direction]);

  useEffect(() => {
    if (documentPage > documentTable.totalPages) setDocumentPage(documentTable.totalPages);
  }, [documentPage, documentTable.totalPages]);

  useEffect(() => {
    setTocPage(1);
  }, [tocPageSize, tocSearch, tocSort.key, tocSort.direction]);

  useEffect(() => {
    if (tocPage > tocTable.totalPages) setTocPage(tocTable.totalPages);
  }, [tocPage, tocTable.totalPages]);

  useEffect(() => {
    setXmlPage(1);
  }, [xmlPageSize, xmlSearch, xmlStatusFilter, xmlSort.key, xmlSort.direction]);

  useEffect(() => {
    if (xmlPage > xmlDocumentTable.totalPages) setXmlPage(xmlDocumentTable.totalPages);
  }, [xmlPage, xmlDocumentTable.totalPages]);

  useEffect(() => {
    if (!selectedDocument?.has_toc) {
      setTocEntries([]);
      return;
    }
    loadToc(selectedDocument.id);
  }, [selectedDocument?.id, selectedDocument?.has_toc]);

  useEffect(() => {
    if (!selectedDocument?.has_xml) {
      setXmlStats(null);
      setXmlPreview("");
      return;
    }
    loadXmlDetails(selectedDocument.id);
  }, [selectedDocument?.id, selectedDocument?.has_xml]);

  useEffect(() => {
    if (!activeJob?.job_id) return;

    let cancelled = false;
    const poll = async () => {
      try {
        const status = await getJobStatus(activeJob.job_id);
        if (cancelled) return;
        setJobStatus(status);
        try {
          const stream = await getProcessStream(status.document_id || activeJob.document_id);
          if (!cancelled) setProcessStream(stream);
        } catch {
          if (!cancelled) setProcessStream("");
        }

        if (["finished", "failed"].includes(status.status)) {
          setActiveJob(null);
          await refreshDocuments(status.document_id || activeJob.document_id);
          await loadActivityLogs();
          if (status.status === "finished") {
            setNotice({ type: "success", message: "Processing completed. Final PDF is ready." });
          } else {
            setNotice({ type: "error", message: status.error || "Processing failed" });
          }
          return;
        }
      } catch (error) {
        if (!cancelled) setNotice({ type: "error", message: error.message });
      }
    };

    poll();
    const timer = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeJob?.job_id]);

  async function loadActivityLogs() {
    try {
      setActivityLoading(true);
      const logs = await getActivityLogs(30);
      setActivityLogs(logs);
    } catch {
      setActivityLogs([]);
    } finally {
      setActivityLoading(false);
    }
  }

  async function refreshDocuments(preferredId) {
    try {
      setLoading(true);
      const records = await listDocuments();
      setDocuments(records);
      const visibleRecords = records.filter((record) => workflowForDocument(record) === activeView);
      if (preferredId) {
        setSelectedId(preferredId);
      } else if (!selectedId && visibleRecords.length) {
        setSelectedId(visibleRecords[0].id);
      } else if (selectedId && !visibleRecords.some((record) => record.id === selectedId)) {
        setSelectedId(visibleRecords[0]?.id || "");
      }
    } catch (error) {
      await refreshDocuments(documentId);
      setNotice({ type: "error", message: error.message });
    } finally {
      setLoading(false);
    }
  }

  async function loadToc(documentId) {
    try {
      const entries = await getToc(documentId);
      setTocEntries(entries);
    } catch {
      setTocEntries([]);
    }
  }

  async function loadXmlDetails(documentId) {
    try {
      setXmlDetailsLoading(true);
      const [stats, preview] = await Promise.all([getXmlStats(documentId), getXmlPreview(documentId)]);
      setXmlStats(stats);
      setXmlPreview(preview);
    } catch {
      setXmlStats(null);
      setXmlPreview("");
    } finally {
      setXmlDetailsLoading(false);
    }
  }

  async function handleUpload(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setNotice({ type: "error", message: "Select a PDF file" });
      return;
    }

    try {
      setNotice(null);
      setUploadProgress(0);
      const result = await uploadDocument(file, setUploadProgress, activeView);
      await refreshDocuments(result.document_id);
      await loadActivityLogs();
      setNotice({ type: "success", message: "PDF uploaded to the workspace." });
    } catch (error) {
      setNotice({ type: "error", message: error.message });
    } finally {
      setUploadProgress(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function startJob(kind, documentId = selectedDocument?.id) {
    if (!documentId) return;

    try {
      setNotice(null);
      setSelectedId(documentId);
      const result =
        kind === "repair"
          ? await repairExistingToc(documentId)
          : await generateToc(documentId);
      setActiveJob({ ...result, kind });
      setJobStatus(result);
      setProcessStream("");
      setNotice({ type: "success", message: "Job queued. The worker is preparing the document." });
      await refreshDocuments(documentId);
      await loadActivityLogs();
    } catch (error) {
      setNotice({ type: "error", message: error.message });
    }
  }

  async function handleDeleteDocument(document) {
    if (!document) return;
    if (activeJob?.document_id === document.id || ["queued", "processing"].includes(lifecycleStatus(document.status))) {
      setNotice({ type: "error", message: "This document is queued or processing and cannot be deleted yet." });
      return;
    }

    const confirmed = window.confirm(
      `Delete "${document.filename}"?\n\nThis removes the source PDF, generated PDF, TOC data, and XML output from local storage.`
    );
    if (!confirmed) return;

    try {
      setDeletingId(document.id);
      setNotice(null);
      await deleteDocument(document.id);
      if (selectedId === document.id) {
        setSelectedId("");
        setTocEntries([]);
        setXmlStats(null);
        setXmlPreview("");
      }
      await refreshDocuments();
      await loadActivityLogs();
      setNotice({ type: "success", message: "Document deleted." });
    } catch (error) {
      setNotice({ type: "error", message: error.message });
    } finally {
      setDeletingId("");
    }
  }

  async function handleConvertXml(documentId = selectedDocument?.id) {
    if (!documentId) return;

    try {
      setXmlConvertingId(documentId);
      setXmlConverting(true);
      setNotice(null);
      setSelectedId(documentId);
      const stats = await convertXml(documentId);
      setXmlStats(stats);
      const preview = await getXmlPreview(documentId);
      setXmlPreview(preview);
      await refreshDocuments(documentId);
      await loadActivityLogs();
      setNotice({ type: "success", message: "PDF converted to XML successfully." });
    } catch (error) {
      setNotice({ type: "error", message: error.message });
    } finally {
      setXmlConvertingId("");
      setXmlConverting(false);
    }
  }

  function handleDocumentSort(key) {
    setDocumentSort((current) => nextSort(current, key));
  }

  function handleTocSort(key) {
    setTocSort((current) => nextSort(current, key));
  }

  function handleXmlSort(key) {
    setXmlSort((current) => nextSort(current, key));
  }

  function onDrop(event) {
    event.preventDefault();
    setIsDragging(false);
    handleUpload(event.dataTransfer.files?.[0]);
  }

  const processing = Boolean(activeJob);

  return (
    <div className="app-shell">
      <Sidebar activeView={activeView} metrics={metrics} onViewChange={setActiveView} processing={processing} />

      <main className="main">
        <Topbar
          activeView={activeView}
          activityLogs={activityLogs}
          activityLoading={activityLoading}
          notificationsOpen={notificationsOpen}
          onActivityRefresh={loadActivityLogs}
          onCloseNotifications={() => setNotificationsOpen(false)}
          onOpenActivity={() => {
            setActiveView("activity");
            setNotificationsOpen(false);
          }}
          onRefresh={() => (isActivityView ? loadActivityLogs() : refreshDocuments(selectedDocument?.id))}
          onToggleNotifications={() => setNotificationsOpen((current) => !current)}
          onUpload={() => fileInputRef.current?.click()}
        />

        {notice && <Notice notice={notice} />}

        {isActivityView ? (
          <ActivityLogsPage logs={activityLogs} loading={activityLoading} onRefresh={loadActivityLogs} />
        ) : (
          <>
            <MetricGrid metrics={metrics} />

            {activeView === "toc" ? (
          <>
            <section className="workspace-grid" id="workspace">
              <UploadCard
                fileInputRef={fileInputRef}
                uploadProgress={uploadProgress}
                isDragging={isDragging}
                setIsDragging={setIsDragging}
                onDrop={onDrop}
                onUpload={handleUpload}
              />
            </section>

            <section className="table-panel" aria-labelledby="live-process-title">
              <div className="panel-header compact">
                <div>
                  <p className="eyebrow">Live Logs</p>
                  <h2 id="live-process-title">TOC Processing Stream</h2>
                </div>
                {selectedDocument?.id ? (
                  <a
                    className="secondary-button"
                    href={getUnresolvedReportDownloadUrl(selectedDocument.id)}
                    onClick={(event) => event.stopPropagation()}
                  >
                    <ArrowDownToLine size={16} />
                    Unresolved Links Report
                  </a>
                ) : null}
              </div>
              <pre className="xml-preview">
                {processStream || (processing ? "Waiting for worker logs..." : "Start TOC generation or hyperlinking to see live logs here.")}
              </pre>
            </section>

            <section className="content-grid">
              <DocumentsPanel
                activeJob={activeJob}
                allCount={tocDocuments.length}
                deletingId={deletingId}
                documents={documentTable.rows}
                filteredCount={documentTable.filteredCount}
                jobStatus={jobStatus}
                loading={loading}
                onDelete={handleDeleteDocument}
                onPageChange={setDocumentPage}
                onPageSizeChange={setDocumentPageSize}
                onProcess={(documentId) => startJob("smart", documentId)}
                onSearch={setDocumentSearch}
                selectedId={selectedDocument?.id}
                onSort={handleDocumentSort}
                onStatusFilter={setDocumentStatusFilter}
                page={documentTable.page}
                pageSize={documentPageSize}
                processing={processing}
                onSelect={setSelectedId}
                search={documentSearch}
                sort={documentSort}
                startIndex={documentTable.startIndex}
                statusFilter={documentStatusFilter}
                totalPages={documentTable.totalPages}
              />
            </section>
          </>
        ) : (
          <XmlConversionPage
            allCount={xmlDocuments.length}
            deletingId={deletingId}
            documents={xmlDocuments}
            documentRows={xmlDocumentTable.rows}
            document={selectedDocument}
            fileInputRef={fileInputRef}
            filteredCount={xmlDocumentTable.filteredCount}
            isDragging={isDragging}
            loading={xmlDetailsLoading}
            onDelete={handleDeleteDocument}
            onDrop={onDrop}
            onPageChange={setXmlPage}
            onPageSizeChange={setXmlPageSize}
            preview={xmlPreview}
            page={xmlDocumentTable.page}
            pageSize={xmlPageSize}
            stats={xmlStats}
            converting={xmlConverting}
            convertingId={xmlConvertingId}
            onConvert={handleConvertXml}
            onRefresh={loadXmlDetails}
            onSearch={setXmlSearch}
            onSelect={setSelectedId}
            onSort={handleXmlSort}
            onStatusFilter={setXmlStatusFilter}
            onUpload={handleUpload}
            search={xmlSearch}
            setIsDragging={setIsDragging}
            sort={xmlSort}
            startIndex={xmlDocumentTable.startIndex}
            statusFilter={xmlStatusFilter}
            totalPages={xmlDocumentTable.totalPages}
            uploadProgress={uploadProgress}
          />
        )}
          </>
        )}
      </main>
    </div>
  );
}

function Sidebar({ activeView, metrics, processing, onViewChange }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <FileText size={22} />
        </div>
        <div>
          <strong>DocuMAP AI</strong>
          <span>Smart Document Mapping Platform</span>
        </div>
      </div>

      <nav className="side-nav" aria-label="Primary">
        {NAV_ITEMS.map((item) => (
          <button
            className={activeView === item.id ? "active" : ""}
            type="button"
            onClick={() => onViewChange(item.id)}
            key={item.id}
          >
            <item.icon size={18} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-stack">
        <div className="sidebar-stat">
          <span>Processed</span>
          <strong>{metrics.completed}</strong>
        </div>
        <div className="sidebar-panel">
          <ShieldCheck size={18} />
          <div>
            <strong>{processing ? "Worker Active" : "Secure Workspace"}</strong>
            <span>{processing ? "Polling job queue" : "Local document vault"}</span>
          </div>
        </div>
      </div>
    </aside>
  );
}

function Topbar({
  activeView,
  activityLogs,
  activityLoading,
  notificationsOpen,
  onActivityRefresh,
  onCloseNotifications,
  onOpenActivity,
  onRefresh,
  onToggleNotifications,
  onUpload,
}) {
  const isXmlView = activeView === "xml";
  const isActivityView = activeView === "activity";
  const title = isActivityView ? "Activity Logs" : isXmlView ? "PDF to XML Conversion" : "TOC and Hyperlinking";
  return (
    <header className="topbar">
      <div className="topbar-copy">
        <p className="eyebrow">Operations Console</p>
        <h1>{title}</h1>
      </div>
      <div className="topbar-actions">
        <button className="icon-button" type="button" onClick={onRefresh} title="Refresh documents">
          <RefreshCw size={18} />
        </button>
        <NotificationMenu
          logs={activityLogs}
          loading={activityLoading}
          open={notificationsOpen}
          onClose={onCloseNotifications}
          onOpenActivity={onOpenActivity}
          onRefresh={onActivityRefresh}
          onToggle={onToggleNotifications}
        />
        {!isActivityView && (
          <button className="primary-button" type="button" onClick={onUpload}>
            <UploadCloud size={18} />
            Upload PDF
          </button>
        )}
      </div>
    </header>
  );
}

function NotificationMenu({ logs, loading, open, onClose, onOpenActivity, onRefresh, onToggle }) {
  const notifications = logs.slice(0, 8);

  return (
    <div className="notification-menu">
      <button
        className={`icon-button notification-button ${open ? "active" : ""}`}
        type="button"
        onClick={onToggle}
        title="Notifications"
        aria-label="Notifications"
        aria-expanded={open}
      >
        <Bell size={18} />
        {!!notifications.length && <span>{Math.min(notifications.length, 9)}</span>}
      </button>

      {open && (
        <div className="notification-popover" role="dialog" aria-label="Recent notifications">
          <div className="notification-head">
            <div>
              <strong>Notifications</strong>
              <span>Recent workflow stages</span>
            </div>
            <button className="icon-button" type="button" onClick={onRefresh} title="Refresh notifications">
              {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
            </button>
          </div>

          {!notifications.length && (
            <div className="notification-empty">
              <ClipboardList size={18} />
              <span>No notifications yet</span>
            </div>
          )}

          {!!notifications.length && (
            <div className="notification-list">
              {notifications.map((log) => (
                <article className={`notification-item ${log.status || "info"}`} key={log.id}>
                  <span className="notification-dot" />
                  <div>
                    <strong>{notificationTitle(log)}</strong>
                    <span>{formatDate(log.created_at)}</span>
                  </div>
                </article>
              ))}
            </div>
          )}

          <div className="notification-footer">
            <button type="button" onClick={onOpenActivity}>
              View all logs
            </button>
            <button type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SystemStatus({ activeJob, documents }) {
  return (
    <section className="system-strip" aria-label="System status">
      <SystemChip icon={Server} label="API" value={API_BASE_URL.replace(/^https?:\/\//, "")} />
      <SystemChip icon={HardDrive} label="Storage" value="Local FS" />
      <SystemChip icon={Layers3} label="Queue" value={activeJob ? "Running" : "Ready"} tone={activeJob ? "busy" : "ready"} />
      <SystemChip icon={Database} label="Records" value={`${documents.length} indexed`} />
    </section>
  );
}

function Notice({ notice }) {
  return (
    <div className={`notice ${notice.type}`} role="status">
      {notice.type === "success" ? <CheckCircle2 size={18} /> : <XCircle size={18} />}
      <span>{notice.message}</span>
    </div>
  );
}

function MetricGrid({ metrics }) {
  return (
    <section className="metric-grid" aria-label="Processing summary">
      <MetricCard icon={FolderOpen} label="Library" value={metrics.total} accent="cyan" trend="Workspace records" />
      <MetricCard icon={FileCheck2} label="Ready PDFs" value={metrics.completed} accent="green" trend="Downloadable output" />
      <MetricCard icon={Database} label="XML Exports" value={metrics.xmlCount} accent="violet" trend="Converted documents" />
      <MetricCard icon={Activity} label="Needs Review" value={metrics.failed} accent="rose" trend={`${metrics.processing} processing`} />
    </section>
  );
}

function ActivityLogsPage({ logs, loading, onRefresh }) {
  const successCount = logs.filter((log) => log.status === "success").length;
  const errorCount = logs.filter((log) => log.status === "error").length;

  return (
    <section className="activity-page" aria-labelledby="activity-page-title">
      <div className="activity-page-head">
        <div>
          <p className="eyebrow">Audit Trail</p>
          <h2 id="activity-page-title">Activity Logs</h2>
        </div>
        <div className="activity-summary">
          <span>{logs.length} recent</span>
          <span>{successCount} completed</span>
          <span>{errorCount} errors</span>
        </div>
      </div>
      <ActivityLogPanel logs={logs} loading={loading} onRefresh={onRefresh} />
    </section>
  );
}

function ActivityLogPanel({ logs, loading, onRefresh }) {
  return (
    <section className="activity-panel" aria-labelledby="activity-title">
      <div className="panel-header compact">
        <div>
          <p className="eyebrow">Activity</p>
          <h2 id="activity-title">System Logs</h2>
        </div>
        <button className="icon-button" type="button" onClick={onRefresh} title="Refresh activity logs">
          {loading ? <Loader2 className="spin" size={17} /> : <RefreshCw size={17} />}
        </button>
      </div>

      {!logs.length && (
        <div className="activity-empty">
          <ClipboardList size={18} />
          <span>No activity recorded yet</span>
        </div>
      )}

      {!!logs.length && (
        <div className="activity-list">
          {logs.map((log) => (
            <article className={`activity-item ${log.status || "info"}`} key={log.id}>
              <span className="activity-dot" />
              <div>
                <strong>{log.message}</strong>
                <span>
                  {formatDate(log.created_at)}
                  {log.workflow ? ` - ${workflowLabel(log.workflow)}` : ""}
                </span>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function UploadCard({
  fileInputRef,
  uploadProgress,
  isDragging,
  setIsDragging,
  onDrop,
  onUpload,
  title = "Upload Source PDF",
  dropTitle = "Drop your MEL PDF here",
  dropDescription = "Drop it here or click this area to add it to the processing queue.",
  tags = ["PDF only", "100 MB max"],
}) {
  return (
    <section
      className={`upload-zone ${isDragging ? "dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={onDrop}
      aria-labelledby="upload-title"
    >
      <input
        ref={fileInputRef}
        type="file"
        accept="application/pdf"
        hidden
        onChange={(event) => onUpload(event.target.files?.[0])}
      />

      <div className="upload-card-header">
        <div className="upload-icon">
          <UploadCloud size={28} />
        </div>
        <div>
          <p className="eyebrow">Ingest</p>
          <h2 id="upload-title">{title}</h2>
        </div>
      </div>
      <div className="upload-tags" aria-label="Upload capabilities">
        {tags.map((tag) => (
          <span key={tag}>{tag}</span>
        ))}
      </div>

      <button className="drop-surface" type="button" onClick={() => fileInputRef.current?.click()}>
        <Sparkles size={18} />
        <strong>{dropTitle}</strong>
        <span>{dropDescription}</span>
      </button>

      {uploadProgress !== null && (
        <div className="progress-wrap">
          <div className="progress-copy">
            <span>Uploading</span>
            <strong>{uploadProgress}%</strong>
          </div>
          <div className="progress-track" aria-label="Upload progress">
            <span style={{ width: `${uploadProgress}%` }} />
          </div>
        </div>
      )}
    </section>
  );
}

function DocumentCommandCenter({ document, activeJob, jobStatus, processing, onStartJob }) {
  const documentStatus = lifecycleStatus(activeJob?.document_id === document?.id ? jobStatus?.status || document?.status : document?.status);
  return (
    <section className="document-panel" aria-labelledby="selected-document-title">
      <div className="selected-document-hero">
        <div className="file-emblem">
          <FileText size={24} />
        </div>
        <div>
          <p className="eyebrow">Selected Document</p>
          <h2 id="selected-document-title">{document ? document.filename : "No document selected"}</h2>
          <span>{document?.id || "Upload or select a record"}</span>
        </div>
        <StatusPill status={documentStatus || "idle"} />
      </div>

      <div className="document-meta">
        <MetaItem label="Document ID" value={document?.id || "-"} mono />
        <MetaItem label="Updated" value={formatDate(document?.updated_at)} />
        <MetaItem label="Output" value={document?.has_output ? "Available" : "Not ready"} />
      </div>

      <div className="action-row" aria-label="Document actions">
        <button
          className="command-button smart-command"
          type="button"
          disabled={!document || processing || ["queued", "processing"].includes(documentStatus)}
          onClick={() => onStartJob("smart")}
        >
          {processing && activeJob?.kind === "smart" ? <Loader2 className="spin" size={20} /> : <WandSparkles size={20} />}
          <span>
            <strong>Smart Process</strong>
            <small>Detect, generate, link</small>
          </span>
        </button>
        <button
          className="command-button repair-command"
          type="button"
          disabled={!document || processing || ["queued", "processing"].includes(documentStatus)}
          onClick={() => onStartJob("repair")}
        >
          {processing && activeJob?.kind === "repair" ? <Loader2 className="spin" size={20} /> : <Link2 size={20} />}
          <span>
            <strong>Repair TOC</strong>
            <small>Existing TOC links</small>
          </span>
        </button>
        <a
          className={`command-button download-command ${document?.has_output ? "" : "disabled"}`}
          href={document?.has_output ? getDownloadUrl(document.id) : undefined}
          aria-disabled={!document?.has_output}
        >
          <ArrowDownToLine size={18} />
          <span>
            <strong>Download</strong>
            <small>Final PDF</small>
          </span>
        </a>
      </div>

      {document?.error && (
        <div className="inline-error">
          <XCircle size={16} />
          <span>{document.error}</span>
        </div>
      )}

      <WorkflowStatus document={document} jobStatus={jobStatus} processing={processing} />
    </section>
  );
}

function DocumentsPanel({
  documents,
  allCount,
  deletingId,
  filteredCount,
  loading,
  selectedId,
  activeJob,
  jobStatus,
  processing,
  onSelect,
  onDelete,
  onProcess,
  search,
  onSearch,
  statusFilter,
  onStatusFilter,
  sort,
  onSort,
  page,
  pageSize,
  totalPages,
  startIndex,
  onPageChange,
  onPageSizeChange,
}) {
  return (
    <section className="table-panel documents-panel" id="documents" aria-labelledby="documents-title">
      <div className="panel-header compact">
        <div>
          <p className="eyebrow">Records</p>
          <h2 id="documents-title">Recent Documents</h2>
        </div>
        {loading ? <Loader2 className="spin muted" size={18} /> : <span className="panel-count">{allCount}</span>}
      </div>

      <div className="mb-4 flex flex-col gap-3 rounded-[8px] border border-slate-200 bg-white/80 p-3 shadow-sm lg:flex-row lg:items-center lg:justify-between">
        <label className="flex min-h-11 flex-1 items-center gap-2 rounded-[8px] border border-slate-200 bg-white px-3 text-sm text-slate-500 shadow-sm transition focus-within:border-cyan-400 focus-within:ring-4 focus-within:ring-cyan-100">
          <Search size={16} />
          <input
            className="w-full min-w-0 bg-transparent text-sm font-semibold text-slate-900 outline-none placeholder:text-slate-400"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Search by file name, document ID, status, or user"
          />
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex min-h-11 items-center gap-2 rounded-[8px] border border-slate-200 bg-white px-3 text-sm font-bold text-slate-600 shadow-sm">
            <ListFilter size={16} />
            <select
              className="bg-transparent text-sm font-bold text-slate-800 outline-none"
              value={statusFilter}
              onChange={(event) => onStatusFilter(event.target.value)}
            >
              <option value="all">All status</option>
              <option value="uploaded">Uploaded</option>
              <option value="queued">Queued</option>
              <option value="processing">Processing</option>
              <option value="ready">Ready</option>
              <option value="failed">Failed</option>
            </select>
          </label>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-black uppercase tracking-wide text-slate-500">
            {filteredCount} of {allCount}
          </span>
        </div>
      </div>

      <div className="document-table">
        <div className="document-table-head">
          <SortHeader label="Sr No / Document" sortKey="filename" sort={sort} onSort={onSort} />
          <SortHeader label="Status" sortKey="status" sort={sort} onSort={onSort} />
          <SortHeader label="Pages" sortKey="page_count" sort={sort} onSort={onSort} />
          <SortHeader label="Created By" sortKey="created_at" sort={sort} onSort={onSort} />
          <SortHeader label="Updated By" sortKey="updated_at" sort={sort} onSort={onSort} />
          <span>Action</span>
        </div>
        {documents.length === 0 && <EmptyState icon={FileText} title="No documents yet" description="Upload a PDF to create the first workspace record." />}
        {documents.map((document, index) => (
          <DocumentTableRow
            activeJob={activeJob}
            document={document}
            index={startIndex + index}
            isSelected={document.id === selectedId}
            jobStatus={jobStatus}
            key={document.id}
            deleting={deletingId === document.id}
            onDelete={onDelete}
            onProcess={onProcess}
            onSelect={onSelect}
            processing={processing}
          />
        ))}
      </div>

      <Pagination
        currentCount={documents.length}
        filteredCount={filteredCount}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        page={page}
        pageSize={pageSize}
        startIndex={startIndex}
        totalPages={totalPages}
      />
    </section>
  );
}

function SortHeader({ label, sortKey, sort, onSort }) {
  const active = sort?.key === sortKey;
  const direction = active ? sort.direction : null;

  return (
    <button
      className={`sort-header ${active ? "active" : ""}`}
      type="button"
      onClick={() => onSort(sortKey)}
      aria-label={`Sort by ${label}${active ? ` ${direction}` : ""}`}
    >
      <span>{label}</span>
      <ArrowUpDown className={active && direction === "desc" ? "sort-desc" : ""} size={13} />
    </button>
  );
}

function Pagination({
  currentCount,
  filteredCount,
  page,
  pageSize,
  totalPages,
  startIndex,
  onPageChange,
  onPageSizeChange,
}) {
  const from = filteredCount === 0 ? 0 : startIndex + 1;
  const to = filteredCount === 0 ? 0 : startIndex + currentCount;

  return (
    <div className="pagination-bar">
      <div className="pagination-copy">
        Showing {from} to {to} of {filteredCount}
      </div>
      <div className="pagination-controls">
        <label className="page-size-control">
          Rows
          <select value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
            {[8, 12, 20, 25, 50, 100].map((size) => (
              <option value={size} key={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
        <button className="pagination-button" type="button" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
          <ChevronLeft size={16} />
          Prev
        </button>
        <span className="page-chip">
          {page} / {totalPages}
        </span>
        <button
          className="pagination-button"
          type="button"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          Next
          <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}

function DocumentTableRow({
  document,
  index,
  isSelected,
  activeJob,
  jobStatus,
  processing,
  deleting,
  onSelect,
  onProcess,
  onDelete,
}) {
  const isActiveJob = activeJob?.document_id === document.id;
  const rowStatus = lifecycleStatus(isActiveJob ? jobStatus?.status || document.status : document.status);
  const showWorkflow = isActiveJob || ["queued", "processing", "ready"].includes(rowStatus);

  return (
    <div
      className={`document-table-row ${isSelected ? "selected" : ""} ${showWorkflow ? "with-workflow" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(document.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(document.id);
        }
      }}
    >
      <div className="doc-name-cell">
        <span>{String(index + 1).padStart(2, "0")}</span>
        <div className="row-icon">
          <FileText size={17} />
        </div>
        <div>
          {document.filename}
          <span>{document.id}</span>
        </div>
      </div>
      <StatusPill status={rowStatus} />
      <span className="table-value">{document.page_count ?? "-"}</span>
      <UserStamp name={document.created_by || "System"} date={document.created_at} />
      <UserStamp name={document.updated_by || "System"} date={document.updated_at} />
      <DocumentRowActions
        deleting={deleting}
        document={document}
        isActiveJob={isActiveJob}
        processing={processing}
        onDelete={(event) => {
          event.stopPropagation();
          onDelete(document);
        }}
        onProcess={(event) => {
          event.stopPropagation();
          onProcess(document.id);
        }}
      />
      {showWorkflow && isActiveJob && (
        <div className="row-workflow-wrap">
          <RowWorkflow document={document} isActiveJob={isActiveJob} jobStatus={jobStatus} />
        </div>
      )}
    </div>
  );
}

function UserStamp({ name, date }) {
  return (
    <div className="user-stamp">
      {name}
      <span>{formatDate(date)}</span>
    </div>
  );
}

function DocumentRowActions({ document, isActiveJob, processing, deleting, onProcess, onDelete }) {
  const status = lifecycleStatus(document.status);
  const isRunning = isActiveJob || ["queued", "processing"].includes(status);
  const canDownload = isReadyStatus(status) && document.has_output;

  return (
    <div className="row-actions" aria-label={`Actions for ${document.filename}`} onClick={(event) => event.stopPropagation()}>
      <button
        className="row-icon-action process"
        type="button"
        disabled={processing || isRunning}
        title={isRunning ? "Processing" : "Process document"}
        aria-label={isRunning ? "Processing" : "Process document"}
        data-tooltip={isRunning ? "Processing" : "Process document"}
        onClick={onProcess}
      >
        {isRunning ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
      </button>

      {canDownload ? (
        <a
          className="row-icon-action download"
          href={getDownloadUrl(document.id)}
          title="Download manual"
          aria-label="Download manual"
          data-tooltip="Download manual"
          onClick={(event) => event.stopPropagation()}
        >
          <ArrowDownToLine size={16} />
        </a>
      ) : (
        <button
          className="row-icon-action download"
          type="button"
          disabled
          title="Download available after processing"
          aria-label="Download available after processing"
          data-tooltip="Download available after processing"
        >
          <ArrowDownToLine size={16} />
        </button>
      )}

      <button
        className="row-icon-action delete"
        type="button"
        disabled={deleting || isRunning}
        title={deleting ? "Deleting document" : "Delete document"}
        aria-label={deleting ? "Deleting document" : "Delete document"}
        data-tooltip={deleting ? "Deleting document" : "Delete document"}
        onClick={onDelete}
      >
        {deleting ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
      </button>
    </div>
  );
}

function RowWorkflow({ document, isActiveJob, jobStatus, statusOverride }) {
  const currentStatus = lifecycleStatus(statusOverride || (isActiveJob ? jobStatus?.status || document.status : document.status));
  const ready = document.has_output || document.has_xml || isReadyStatus(currentStatus);
  const queued = currentStatus === "queued" || currentStatus === "processing" || ready;
  const running = currentStatus === "processing";
  const steps = [
    { label: "Uploaded", done: Boolean(document), active: false },
    { label: "Queued", done: queued || running || ready, active: currentStatus === "queued" },
    { label: "Processing", done: ready, active: running && !ready },
    { label: "Ready", done: ready, active: false },
  ];

  return (
    <div className="row-workflow" aria-label="Document processing stages">
      {steps.map((step) => (
        <span className={`${step.done ? "done" : ""} ${step.active ? "active" : ""}`} key={step.label}>
          {step.active ? <Loader2 className="spin" size={13} /> : step.done ? <CheckCircle2 size={13} /> : <span className="dot" />}
          {step.label}
        </span>
      ))}
    </div>
  );
}

function TocPreview({
  entries,
  filteredEntries,
  totalEntries,
  search,
  onSearch,
  sort,
  onSort,
  page,
  pageSize,
  totalPages,
  startIndex,
  onPageChange,
  onPageSizeChange,
}) {
  return (
    <section className="table-panel toc-panel" id="toc-preview" aria-labelledby="toc-title">
      <div className="panel-header compact">
        <div>
          <p className="eyebrow">Output</p>
          <h2 id="toc-title">TOC Preview</h2>
        </div>
      </div>

      <div className="mb-4 flex flex-col gap-3 rounded-[8px] border border-slate-200 bg-white/80 p-3 shadow-sm md:flex-row md:items-center md:justify-between">
        <label className="flex min-h-11 flex-1 items-center gap-2 rounded-[8px] border border-slate-200 bg-white px-3 text-sm text-slate-500 shadow-sm transition focus-within:border-violet-400 focus-within:ring-4 focus-within:ring-violet-100">
          <Search size={16} />
          <input
            className="w-full min-w-0 bg-transparent text-sm font-semibold text-slate-900 outline-none placeholder:text-slate-400"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Search title, page, or level"
          />
        </label>
        <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-black uppercase tracking-wide text-slate-500">
          {filteredEntries} of {totalEntries} rows
        </span>
      </div>

      <div className="toc-table">
        <div className="toc-head">
          <SortHeader label="Title" sortKey="title" sort={sort} onSort={onSort} />
          <SortHeader label="Level" sortKey="level" sort={sort} onSort={onSort} />
          <SortHeader label="Page" sortKey="page" sort={sort} onSort={onSort} />
          <SortHeader label="Score" sortKey="confidence" sort={sort} onSort={onSort} />
        </div>
        {entries.length === 0 && (
          <EmptyState icon={BookOpen} title="No TOC rows" description="Process a document to preview generated bookmarks." />
        )}
        {entries.map((entry, index) => (
          <div className="toc-row" key={`${entry.title}-${entry.page}-${index}`}>
            <span style={{ paddingLeft: `${Math.max(entry.level - 1, 0) * 14}px` }}>{entry.title}</span>
            <span>{entry.level}</span>
            <span>{entry.page}</span>
            <span>{Math.round((entry.confidence || 0) * 100)}%</span>
          </div>
        ))}
      </div>

      <Pagination
        currentCount={entries.length}
        filteredCount={filteredEntries}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        page={page}
        pageSize={pageSize}
        startIndex={startIndex}
        totalPages={totalPages}
      />
    </section>
  );
}

function XmlConversionPage({
  allCount,
  deletingId,
  documents,
  documentRows,
  document,
  fileInputRef,
  filteredCount,
  isDragging,
  loading,
  onDelete,
  onDrop,
  onPageChange,
  onPageSizeChange,
  preview,
  page,
  pageSize,
  stats,
  converting,
  convertingId,
  onConvert,
  onRefresh,
  onSearch,
  onSelect,
  onSort,
  onStatusFilter,
  onUpload,
  search,
  setIsDragging,
  sort,
  startIndex,
  statusFilter,
  totalPages,
  uploadProgress,
}) {
  const canDownloadXml = Boolean(document?.has_xml && stats);
  const selectedXmlStatus = document ? xmlStatusForDocument(document, convertingId) : "idle";
  const statCards = [
    { label: "Pages", value: stats?.page_count ?? document?.page_count ?? "-", hint: "Physical PDF pages" },
    { label: "Words", value: formatNumber(stats?.words), hint: "Extracted text tokens" },
    { label: "Characters", value: formatNumber(stats?.characters), hint: "Span text length" },
    { label: "Text Blocks", value: formatNumber(stats?.text_blocks), hint: "Layout text blocks" },
    { label: "Image Blocks", value: formatNumber(stats?.image_blocks), hint: "Image regions" },
    { label: "XML Size", value: formatBytes(stats?.xml_size_bytes), hint: "Generated output" },
  ];

  return (
    <section className="xml-page" id="xml-workspace" aria-labelledby="xml-title">
      <UploadCard
        fileInputRef={fileInputRef}
        uploadProgress={uploadProgress}
        isDragging={isDragging}
        setIsDragging={setIsDragging}
        onDrop={onDrop}
        onUpload={onUpload}
        title="Upload PDF for XML"
        dropTitle="Drop any PDF for XML conversion"
        dropDescription="Drop a file here or click this area to choose a PDF for XML conversion."
        tags={["PDF only", "Layout XML", "Text spans"]}
      />

      <XmlDocumentsPanel
        allCount={allCount}
        converting={converting}
        convertingId={convertingId}
        deletingId={deletingId}
        documents={documentRows}
        filteredCount={filteredCount}
        onConvert={onConvert}
        onDelete={onDelete}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        onSearch={onSearch}
        onSelect={onSelect}
        onSort={onSort}
        onStatusFilter={onStatusFilter}
        page={page}
        pageSize={pageSize}
        search={search}
        selectedId={document?.id}
        sort={sort}
        startIndex={startIndex}
        statusFilter={statusFilter}
        totalPages={totalPages}
      />

      <div className="xml-command-panel">
        <div className="selected-document-hero">
          <div className="file-emblem">
            <Database size={24} />
          </div>
          <div>
            <p className="eyebrow">Structured Export</p>
            <h2 id="xml-title">{document ? document.filename : "Select a document"}</h2>
            <span>{document?.id || "Upload a source PDF before conversion"}</span>
          </div>
          <StatusPill status={selectedXmlStatus} />
        </div>

        <div className="xml-controls">
          <label className="xml-select-wrap">
            <span>Source PDF</span>
            <select value={document?.id || ""} onChange={(event) => onSelect(event.target.value)}>
              {!documents.length && <option value="">No documents uploaded</option>}
              {documents.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.filename}
                </option>
              ))}
            </select>
          </label>

          <button
            className="primary-button"
            type="button"
            disabled={!document || converting || ["queued", "processing"].includes(selectedXmlStatus)}
            onClick={() => onConvert(document.id)}
          >
            {converting ? <Loader2 className="spin" size={18} /> : <Sparkles size={18} />}
            Convert XML
          </button>

          <button
            className="secondary-button"
            type="button"
            disabled={!document?.has_xml || loading}
            onClick={() => onRefresh(document.id)}
          >
            {loading ? <Loader2 className="spin" size={17} /> : <RefreshCw size={17} />}
            Refresh Stats
          </button>

          <a className={`secondary-button ${canDownloadXml ? "" : "disabled"}`} href={canDownloadXml ? getXmlDownloadUrl(document.id) : undefined}>
            <ArrowDownToLine size={17} />
            Download XML
          </a>
        </div>
      </div>

      <div className="xml-stat-grid">
        {statCards.map((card) => (
          <XmlStatCard key={card.label} {...card} />
        ))}
      </div>

      <div className="xml-detail-grid">
        <section className="table-panel xml-font-panel" aria-labelledby="xml-fonts-title">
          <div className="panel-header compact">
            <div>
              <p className="eyebrow">Typography</p>
              <h2 id="xml-fonts-title">Fonts Detected</h2>
            </div>
            <span className="panel-count">{stats?.fonts?.length || 0}</span>
          </div>
          {stats?.fonts?.length ? (
            <div className="xml-font-list">
              {stats.fonts.map((font) => (
                <span key={font}>{font}</span>
              ))}
            </div>
          ) : (
            <EmptyState icon={FileText} title="No font data yet" description="Convert a PDF to inspect extracted font families." />
          )}
        </section>

        <section className="table-panel xml-preview-panel" aria-labelledby="xml-preview-title">
          <div className="panel-header compact">
            <div>
              <p className="eyebrow">XML Preview</p>
              <h2 id="xml-preview-title">Generated Structure</h2>
            </div>
            <span className="xml-preview-note">First 16k characters</span>
          </div>
          <pre className="xml-preview">{loading ? "Loading XML preview..." : preview || "Convert a document to preview XML here."}</pre>
        </section>
      </div>

      <section className="table-panel xml-page-table-panel" aria-labelledby="xml-pages-title">
        <div className="panel-header compact">
          <div>
            <p className="eyebrow">Page Analytics</p>
            <h2 id="xml-pages-title">Per-Page Conversion Details</h2>
          </div>
          <span className="panel-count">{stats?.pages?.length || 0}</span>
        </div>
        <div className="xml-page-table">
          <div className="xml-page-head">
            <span>Page</span>
            <span>Size</span>
            <span>Text Blocks</span>
            <span>Images</span>
            <span>Lines</span>
            <span>Spans</span>
            <span>Words</span>
            <span>Links</span>
            <span>Annots</span>
          </div>
          {!stats?.pages?.length && (
            <EmptyState icon={Database} title="No page statistics" description="Run PDF to XML conversion to see page-by-page extraction details." />
          )}
          {stats?.pages?.map((page) => (
            <div className="xml-page-row" key={page.page}>
              <span>{page.page}</span>
              <span>
                {Math.round(page.width)} x {Math.round(page.height)}
              </span>
              <span>{formatNumber(page.text_blocks)}</span>
              <span>{formatNumber(page.image_blocks)}</span>
              <span>{formatNumber(page.lines)}</span>
              <span>{formatNumber(page.spans)}</span>
              <span>{formatNumber(page.words)}</span>
              <span>{formatNumber(page.links)}</span>
              <span>{formatNumber(page.annotations)}</span>
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}

function XmlDocumentsPanel({
  allCount,
  converting,
  convertingId,
  deletingId,
  documents,
  filteredCount,
  onConvert,
  onDelete,
  onPageChange,
  onPageSizeChange,
  onSearch,
  onSelect,
  onSort,
  onStatusFilter,
  page,
  pageSize,
  search,
  selectedId,
  sort,
  startIndex,
  statusFilter,
  totalPages,
}) {
  return (
    <section className="table-panel xml-documents-panel" aria-labelledby="xml-documents-title">
      <div className="panel-header compact">
        <div>
          <p className="eyebrow">XML Queue</p>
          <h2 id="xml-documents-title">Uploaded PDF Files</h2>
        </div>
        <span className="panel-count">{allCount}</span>
      </div>

      <div className="mb-4 flex flex-col gap-3 rounded-[8px] border border-slate-200 bg-white/80 p-3 shadow-sm lg:flex-row lg:items-center lg:justify-between">
        <label className="flex min-h-11 flex-1 items-center gap-2 rounded-[8px] border border-slate-200 bg-white px-3 text-sm text-slate-500 shadow-sm transition focus-within:border-violet-400 focus-within:ring-4 focus-within:ring-violet-100">
          <Search size={16} />
          <input
            className="w-full min-w-0 bg-transparent text-sm font-semibold text-slate-900 outline-none placeholder:text-slate-400"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Search XML files by name, ID, status, or user"
          />
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex min-h-11 items-center gap-2 rounded-[8px] border border-slate-200 bg-white px-3 text-sm font-bold text-slate-600 shadow-sm">
            <ListFilter size={16} />
            <select
              className="bg-transparent text-sm font-bold text-slate-800 outline-none"
              value={statusFilter}
              onChange={(event) => onStatusFilter(event.target.value)}
            >
              <option value="all">All XML status</option>
              <option value="uploaded">Uploaded</option>
              <option value="queued">Queued</option>
              <option value="processing">Processing</option>
              <option value="ready">Ready</option>
              <option value="failed">Failed</option>
            </select>
          </label>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-black uppercase tracking-wide text-slate-500">
            {filteredCount} of {allCount}
          </span>
        </div>
      </div>

      <div className="xml-document-table">
        <div className="xml-document-head">
          <SortHeader label="Sr No / Document" sortKey="filename" sort={sort} onSort={onSort} />
          <SortHeader label="Status" sortKey="xml_status" sort={sort} onSort={onSort} />
          <SortHeader label="Pages" sortKey="page_count" sort={sort} onSort={onSort} />
          <SortHeader label="Uploaded" sortKey="created_at" sort={sort} onSort={onSort} />
          <SortHeader label="Updated" sortKey="updated_at" sort={sort} onSort={onSort} />
          <span>Action</span>
        </div>
        {!documents.length && (
          <EmptyState icon={Database} title="No XML records found" description="Upload a PDF or adjust search and filters." />
        )}
        {documents.map((document, index) => (
          <XmlDocumentRow
            converting={convertingId === document.id}
            convertingAny={converting}
            deleting={deletingId === document.id}
            document={document}
            index={startIndex + index}
            isSelected={document.id === selectedId}
            key={document.id}
            onConvert={onConvert}
            onDelete={onDelete}
            onSelect={onSelect}
          />
        ))}
      </div>

      <Pagination
        currentCount={documents.length}
        filteredCount={filteredCount}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        page={page}
        pageSize={pageSize}
        startIndex={startIndex}
        totalPages={totalPages}
      />
    </section>
  );
}

function XmlDocumentRow({ document, index, isSelected, converting, convertingAny, deleting, onSelect, onConvert, onDelete }) {
  const rowStatus = document.xml_status;
  const showWorkflow = ["queued", "processing", "ready"].includes(rowStatus);
  return (
    <div
      className={`xml-document-row ${isSelected ? "selected" : ""} ${showWorkflow ? "with-workflow" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(document.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(document.id);
        }
      }}
    >
      <div className="doc-name-cell">
        <span>{String(index + 1).padStart(2, "0")}</span>
        <div className="row-icon">
          <Database size={17} />
        </div>
        <div>
          {document.filename}
          <span>{document.id}</span>
        </div>
      </div>
      <XmlStatusPill status={rowStatus} />
      <span className="table-value">{document.page_count ?? "-"}</span>
      <UserStamp name={document.created_by || "System"} date={document.created_at} />
      <UserStamp name={document.updated_by || "System"} date={document.updated_at} />
      <XmlRowActions
        converting={converting}
        convertingAny={convertingAny}
        deleting={deleting}
        document={document}
        status={rowStatus}
        onConvert={(event) => {
          event.stopPropagation();
          onConvert(document.id);
        }}
        onDelete={(event) => {
          event.stopPropagation();
          onDelete(document);
        }}
      />
      {showWorkflow && (
        <div className="row-workflow-wrap">
          <RowWorkflow document={document} statusOverride={rowStatus} />
        </div>
      )}
    </div>
  );
}

function XmlRowActions({ document, status, converting, convertingAny, deleting, onConvert, onDelete }) {
  const canDownload = Boolean(document.has_xml);
  const isRunning = converting || ["queued", "processing"].includes(status);

  return (
    <div className="row-actions" aria-label={`XML actions for ${document.filename}`} onClick={(event) => event.stopPropagation()}>
      <button
        className="row-icon-action process"
        type="button"
        disabled={convertingAny || isRunning}
        title={converting ? "Converting to XML" : convertingAny ? "Conversion in progress" : "Convert to XML"}
        aria-label={converting ? "Converting to XML" : convertingAny ? "Conversion in progress" : "Convert to XML"}
        data-tooltip={converting ? "Converting to XML" : convertingAny ? "Conversion in progress" : "Convert to XML"}
        onClick={onConvert}
      >
        {converting ? <Loader2 className="spin" size={16} /> : <FileCog2 size={16} />}
      </button>

      {canDownload ? (
        <a
          className="row-icon-action download"
          href={getXmlDownloadUrl(document.id)}
          title="Download XML"
          aria-label="Download XML"
          data-tooltip="Download XML"
          onClick={(event) => event.stopPropagation()}
        >
          <ArrowDownToLine size={16} />
        </a>
      ) : (
        <button
          className="row-icon-action download"
          type="button"
          disabled
          title="Download available after XML conversion"
          aria-label="Download available after XML conversion"
          data-tooltip="Download available after XML conversion"
        >
          <ArrowDownToLine size={16} />
        </button>
      )}

      <button
        className="row-icon-action delete"
        type="button"
        disabled={deleting || isRunning}
        title={deleting ? "Deleting document" : "Delete document"}
        aria-label={deleting ? "Deleting document" : "Delete document"}
        data-tooltip={deleting ? "Deleting document" : "Delete document"}
        onClick={onDelete}
      >
        {deleting ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
      </button>
    </div>
  );
}

function XmlStatusPill({ status }) {
  return <span className={`xml-status-pill ${status}`}>{xmlStatusLabel(status)}</span>;
}

function XmlStatCard({ label, value, hint }) {
  return (
    <article className="xml-stat-card">
      <span>{hint}</span>
      <strong>{value ?? "-"}</strong>
      <small>{label}</small>
    </article>
  );
}

function MetricCard({ icon: Icon, label, value, accent, trend }) {
  return (
    <article className={`metric-card ${accent}`}>
      <div className="metric-card-top">
        <div className="metric-icon">
          <Icon size={20} />
        </div>
        <span>{trend}</span>
      </div>
      <div>
        <span className="metric-label">{label}</span>
        <strong>{value}</strong>
      </div>
    </article>
  );
}

function SystemChip({ icon: Icon, label, value, tone }) {
  return (
    <div className={`system-chip ${tone || ""}`}>
      <Icon size={16} />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function MetaItem({ label, value, mono }) {
  return (
    <div className="meta-item">
      <span>{label}</span>
      <strong className={mono ? "mono" : ""}>{value}</strong>
    </div>
  );
}

function StatusPill({ status }) {
  return <span className={`status-pill ${status}`}>{statusLabel(status)}</span>;
}

function WorkflowStatus({ document, jobStatus, processing }) {
  const currentStatus = lifecycleStatus(jobStatus?.status || document?.status);
  const ready = Boolean(document?.has_output || isReadyStatus(currentStatus));
  const steps = [
    { label: "Uploaded", done: Boolean(document), active: false },
    { label: "Queued", done: ["queued", "processing"].includes(currentStatus) || ready, active: processing && currentStatus === "queued" },
    { label: "Processing", done: ready, active: processing && currentStatus === "processing" },
    { label: "Ready", done: ready, active: false },
  ];

  return (
    <div className="workflow">
      {steps.map((step) => (
        <div className={`workflow-step ${step.done ? "done" : ""} ${step.active ? "active" : ""}`} key={step.label}>
          <span>
            {step.done ? <CheckCircle2 size={15} /> : step.active ? <Loader2 className="spin" size={15} /> : <Play size={15} />}
          </span>
          <strong>{step.label}</strong>
        </div>
      ))}
    </div>
  );
}

function EmptyState({ icon: Icon, title, description }) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <Icon size={22} />
      </div>
      <strong>{title}</strong>
      {description && <span>{description}</span>}
    </div>
  );
}

function nextSort(current, key) {
  if (current.key === key) {
    return { key, direction: current.direction === "asc" ? "desc" : "asc" };
  }

  const defaultDirection = ["filename", "title", "status"].includes(key) ? "asc" : "desc";
  return { key, direction: defaultDirection };
}

function sortItems(items, sort) {
  const modifier = sort.direction === "asc" ? 1 : -1;
  return [...items].sort((first, second) => {
    const firstValue = comparableValue(first, sort.key);
    const secondValue = comparableValue(second, sort.key);

    if (typeof firstValue === "number" && typeof secondValue === "number") {
      return (firstValue - secondValue) * modifier;
    }

    return String(firstValue).localeCompare(String(secondValue), undefined, {
      numeric: true,
      sensitivity: "base",
    }) * modifier;
  });
}

function comparableValue(item, key) {
  const value = item?.[key];

  if (["created_at", "updated_at"].includes(key)) {
    return value ? new Date(value).getTime() : 0;
  }

  if (["confidence", "level", "page", "page_count"].includes(key)) {
    return Number(value || 0);
  }

  return `${value || ""}`.toLowerCase();
}

function lifecycleStatus(status) {
  const normalized = `${status || ""}`.toLowerCase();
  const aliases = {
    completed: "ready",
    converted: "ready",
    finished: "ready",
    started: "processing",
    converting: "processing",
    pending: "uploaded",
  };
  return aliases[normalized] || normalized;
}

function isReadyStatus(status) {
  return lifecycleStatus(status) === "ready";
}

function workflowForDocument(document) {
  if (document?.workflow) return document.workflow;
  return document?.has_xml && !document?.has_output && !document?.has_toc ? WORKFLOWS.XML : WORKFLOWS.TOC;
}

function workflowLabel(workflow) {
  const labels = {
    toc: "TOC & Hyperlinking",
    xml: "PDF to XML",
  };
  return labels[workflow] || workflow;
}

function notificationTitle(log) {
  if (!log) return "System activity";
  const stageLabels = {
    document_uploaded: "File uploaded",
    toc_job_queued: "Queued for processing",
    document_status_updated: statusLabel(log.metadata?.status),
    toc_processing_started: "Processing started",
    hyperlink_processing_started: "Processing started",
    xml_conversion_started: "Processing started",
    toc_processing_completed: "Processing complete",
    hyperlink_processing_completed: "Processing complete",
    xml_conversion_completed: "Processing complete",
    xml_conversion_ready: "Ready for download",
    pdf_downloaded: "PDF downloaded",
    xml_downloaded: "XML downloaded",
    document_deleted: "File deleted",
  };
  const stage = stageLabels[log.action] || log.message || "System activity";
  return log.filename ? `${stage}: ${log.filename}` : stage;
}

function xmlStatusForDocument(document, convertingId) {
  if (convertingId === document.id) return "processing";
  if (document.has_xml) return "ready";
  return lifecycleStatus(document.status || "uploaded");
}

function xmlStatusLabel(status) {
  return statusLabel(lifecycleStatus(status));
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "-";
  return new Intl.NumberFormat().format(Number(value));
}

function formatBytes(value) {
  if (!value) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value);
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size >= 10 || unitIndex === 0 ? Math.round(size) : size.toFixed(1)} ${units[unitIndex]}`;
}

function formatDate(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function statusLabel(status) {
  const normalized = lifecycleStatus(status);
  const labels = {
    uploaded: "Uploaded",
    queued: "Queued",
    processing: "Processing",
    ready: "Ready",
    completed: "Ready",
    failed: "Failed",
    idle: "Idle",
    started: "Processing",
    finished: "Ready",
  };
  return labels[normalized] || normalized || status;
}

export default App;
