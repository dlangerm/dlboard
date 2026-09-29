-- A sqlite database written by the pre-SQLAlchemy store (raw SQL strings, commit 125664a), as
-- `sqlite3 .dump` printed it -- see legacy_schema_test.py. Never regenerate: it's frozen history.
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE AppState
    (id INTEGER PRIMARY KEY AUTOINCREMENT,bootstrap_admin_assigned INTEGER NOT NULL DEFAULT false);
INSERT INTO AppState VALUES(1,1);
CREATE TABLE User
    (username TEXT NOT NULL,scopes TEXT NOT NULL,created_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,UNIQUE (username));
INSERT INTO User VALUES('admin','["*"]','2026-02-03T04:05:06.123456Z',1);
INSERT INTO User VALUES('bob','[]','2026-02-03T04:05:06.123456Z',2);
CREATE TABLE Project
    (name TEXT NOT NULL,description TEXT NOT NULL,created_by INTEGER,created_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,deleted_by INTEGER,deleted_at TEXT,FOREIGN KEY (created_by) REFERENCES User(id),FOREIGN KEY (deleted_by) REFERENCES User(id));
INSERT INTO Project VALUES('legacy','from main',1,'2026-01-02T03:04:05Z',1,NULL,NULL);
CREATE TABLE Experiment
    (project_id INTEGER NOT NULL,name TEXT NOT NULL DEFAULT '',description TEXT NOT NULL DEFAULT '',source TEXT,created_by INTEGER,created_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,revision INTEGER NOT NULL DEFAULT 0,notes_revision INTEGER NOT NULL DEFAULT 0,last_activity_at TEXT,deleted_by INTEGER,deleted_at TEXT,FOREIGN KEY (project_id) REFERENCES Project(id) ON DELETE CASCADE,FOREIGN KEY (created_by) REFERENCES User(id),FOREIGN KEY (deleted_by) REFERENCES User(id));
INSERT INTO Experiment VALUES(1,'exp','e','pytorch_lightning',1,'2026-01-02T03:04:05Z',1,5,1,'2026-02-03T04:05:06.654321+00:00',NULL,NULL);
CREATE TABLE Run
    (experiment_id INTEGER NOT NULL,name TEXT,created_by INTEGER,created_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,deleted_by INTEGER,deleted_at TEXT,FOREIGN KEY (experiment_id) REFERENCES Experiment(id) ON DELETE CASCADE,FOREIGN KEY (created_by) REFERENCES User(id),FOREIGN KEY (deleted_by) REFERENCES User(id));
INSERT INTO Run VALUES(1,'r0',1,'2026-01-02T03:04:05Z',1,NULL,NULL);
INSERT INTO Run VALUES(1,'doomed',NULL,'2026-01-02T03:04:05Z',2,1,'2026-02-03T04:05:06.654321+00:00');
CREATE TABLE UnderlyingMetricTableEntry
    (id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT NOT NULL,value REAL,experiment_id INTEGER NOT NULL,run_id INTEGER NOT NULL,step INTEGER NOT NULL,timestamp_utc TEXT NOT NULL,FOREIGN KEY (experiment_id) REFERENCES Experiment(id),FOREIGN KEY (run_id) REFERENCES Run(id) ON DELETE CASCADE);
INSERT INTO UnderlyingMetricTableEntry VALUES(1,'loss',1.0,1,1,0,'2026-01-02T03:04:05Z');
INSERT INTO UnderlyingMetricTableEntry VALUES(2,'acc',NULL,1,1,0,'2026-01-02T03:04:05Z');
INSERT INTO UnderlyingMetricTableEntry VALUES(3,'loss',0.5,1,1,1,'2026-01-02T03:04:06Z');
INSERT INTO UnderlyingMetricTableEntry VALUES(4,'acc',0.5,1,1,1,'2026-01-02T03:04:06Z');
INSERT INTO UnderlyingMetricTableEntry VALUES(5,'loss',0.333333333333333314,1,1,2,'2026-01-02T03:04:07Z');
INSERT INTO UnderlyingMetricTableEntry VALUES(6,'acc',0.5,1,1,2,'2026-01-02T03:04:07Z');
CREATE TABLE HyperParams
    (run_id INTEGER NOT NULL,experiment_id INTEGER NOT NULL,raw_hparams TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,FOREIGN KEY (experiment_id) REFERENCES Experiment(id),FOREIGN KEY (run_id) REFERENCES Run(id) ON DELETE CASCADE);
INSERT INTO HyperParams VALUES(1,1,'{"lr": 0.1, "opt": "adam", "ema": true}',1);
CREATE TABLE Artifact
    (key TEXT NOT NULL,fname TEXT NOT NULL,tags TEXT NOT NULL,run_id INTEGER NOT NULL,experiment_id INTEGER NOT NULL,step INTEGER,created_by INTEGER,created_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,ref TEXT NOT NULL,deleted_by INTEGER,deleted_at TEXT,FOREIGN KEY (experiment_id) REFERENCES Experiment(id),FOREIGN KEY (run_id) REFERENCES Run(id) ON DELETE CASCADE,FOREIGN KEY (created_by) REFERENCES User(id),FOREIGN KEY (deleted_by) REFERENCES User(id));
INSERT INTO Artifact VALUES('img','i.png','{"split": "val"}',1,1,0,1,'2026-01-02T03:04:05Z',1,'file://a',NULL,NULL);
CREATE TABLE Page
    (run_id INTEGER,experiment_id INTEGER,project_id INTEGER,owner_id INTEGER,name TEXT NOT NULL DEFAULT '',panels TEXT NOT NULL,page_settings TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,FOREIGN KEY (run_id) REFERENCES Run(id) ON DELETE CASCADE,FOREIGN KEY (experiment_id) REFERENCES Experiment(id) ON DELETE CASCADE,FOREIGN KEY (project_id) REFERENCES Project(id) ON DELETE CASCADE);
INSERT INTO Page VALUES(NULL,1,NULL,NULL,'','[{"name": "metrics", "tab": "", "charts": [], "sync": true, "layout": "packed"}]','{"open_panel": ["metrics"]}',1);
INSERT INTO Page VALUES(NULL,1,NULL,2,'bob''s view','[]','{}',2);
CREATE TABLE Comment
    (experiment_id INTEGER NOT NULL,author_id INTEGER NOT NULL,body TEXT NOT NULL,run_ids TEXT NOT NULL,mentioned_user_ids TEXT NOT NULL,created_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,FOREIGN KEY (experiment_id) REFERENCES Experiment(id) ON DELETE CASCADE,FOREIGN KEY (author_id) REFERENCES User(id));
INSERT INTO Comment VALUES(1,2,'hi','[1]','[]','2026-01-02T03:04:05Z',1);
CREATE TABLE AuditLogEntry
    (timestamp_utc TEXT NOT NULL,user_id INTEGER NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,details TEXT NOT NULL DEFAULT '{}',id INTEGER PRIMARY KEY AUTOINCREMENT,FOREIGN KEY (user_id) REFERENCES User(id));
INSERT INTO AuditLogEntry VALUES('2026-02-03T04:05:06.123456Z',1,'soft_delete','run',2,'{"Artifact": 0}',1);
CREATE TABLE ArtifactPurgeTask
    (artifact_id INTEGER NOT NULL,ref TEXT NOT NULL,requested_by INTEGER NOT NULL,requested_at TEXT NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,last_error TEXT,FOREIGN KEY (requested_by) REFERENCES User(id));
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('AppState',1);
INSERT INTO sqlite_sequence VALUES('User',2);
INSERT INTO sqlite_sequence VALUES('Project',1);
INSERT INTO sqlite_sequence VALUES('Experiment',1);
INSERT INTO sqlite_sequence VALUES('Run',2);
INSERT INTO sqlite_sequence VALUES('UnderlyingMetricTableEntry',6);
INSERT INTO sqlite_sequence VALUES('HyperParams',1);
INSERT INTO sqlite_sequence VALUES('Artifact',1);
INSERT INTO sqlite_sequence VALUES('Page',2);
INSERT INTO sqlite_sequence VALUES('Comment',1);
INSERT INTO sqlite_sequence VALUES('AuditLogEntry',1);
CREATE INDEX idx_UnderlyingMetricTableEntry_key_experiment_id_run_id_step_timestamp_utc
    ON UnderlyingMetricTableEntry (key,experiment_id,run_id,step,timestamp_utc);
CREATE INDEX idx_Artifact_key_fname_run_id_experiment_id_step_created_by_created_at_ref_deleted_by_deleted_at
    ON Artifact (key,fname,run_id,experiment_id,step,created_by,created_at,ref,deleted_by,deleted_at);
CREATE UNIQUE INDEX idx_Page_shared_run_id
    ON Page (run_id) WHERE owner_id IS NULL;
CREATE UNIQUE INDEX idx_Page_shared_experiment_id
    ON Page (experiment_id) WHERE owner_id IS NULL;
CREATE UNIQUE INDEX idx_Page_shared_project_id
    ON Page (project_id) WHERE owner_id IS NULL;
COMMIT;
