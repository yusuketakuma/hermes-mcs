-- Runtime DDL and initial rows added to shape-18.sql by the clean v1.3.0 Ledger.
-- Source commit afe1796c75770cc29dfdbf16fffc53612de77db6. Synthetic clock 1700000000.0.

CREATE TABLE ledger_relation_guards(
                  relation TEXT PRIMARY KEY,
                  mode TEXT NOT NULL CHECK(mode IN ('shadow','enforce')),
                  existing_count INTEGER NOT NULL,
                  shadow_count INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER g1_artifacts_msg_ins
                        BEFORE INSERT ON artifacts
                        WHEN NEW.message_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=NEW.message_id AND (NEW.project_id IS NULL OR m.project_id=NEW.project_id))
                        BEGIN
                          SELECT RAISE(ABORT,'g1: artifacts message relation')
                          WHERE (SELECT mode FROM ledger_relation_guards
                                 WHERE relation='artifacts')='enforce';
                          UPDATE ledger_relation_guards
                          SET shadow_count=shadow_count+1
                          WHERE relation='artifacts' AND mode='shadow';
                        END;
CREATE TRIGGER g1_artifacts_msg_upd
                        BEFORE UPDATE OF message_id, project_id ON artifacts
                        WHEN NEW.message_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=NEW.message_id AND (NEW.project_id IS NULL OR m.project_id=NEW.project_id))
                        BEGIN
                          SELECT RAISE(ABORT,'g1: artifacts message relation')
                          WHERE (SELECT mode FROM ledger_relation_guards
                                 WHERE relation='artifacts')='enforce';
                          UPDATE ledger_relation_guards
                          SET shadow_count=shadow_count+1
                          WHERE relation='artifacts' AND mode='shadow';
                        END;
CREATE TRIGGER g1_attachments_msg_ins
                        BEFORE INSERT ON attachments
                        WHEN NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=NEW.message_id)
                        BEGIN
                          SELECT RAISE(ABORT,'g1: attachments message relation')
                          WHERE (SELECT mode FROM ledger_relation_guards
                                 WHERE relation='attachments')='enforce';
                          UPDATE ledger_relation_guards
                          SET shadow_count=shadow_count+1
                          WHERE relation='attachments' AND mode='shadow';
                        END;
CREATE TRIGGER g1_attachments_msg_upd
                        BEFORE UPDATE OF message_id ON attachments
                        WHEN NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=NEW.message_id)
                        BEGIN
                          SELECT RAISE(ABORT,'g1: attachments message relation')
                          WHERE (SELECT mode FROM ledger_relation_guards
                                 WHERE relation='attachments')='enforce';
                          UPDATE ledger_relation_guards
                          SET shadow_count=shadow_count+1
                          WHERE relation='attachments' AND mode='shadow';
                        END;
CREATE TABLE ledger_relation_audit(
                  relation TEXT PRIMARY KEY, mode TEXT NOT NULL);
CREATE TRIGGER relation_audit_messages_del
                    AFTER DELETE ON messages WHEN 1
                    BEGIN DELETE FROM ledger_relation_audit; END;
CREATE TRIGGER relation_audit_messages_upd
                    AFTER UPDATE OF message_id, project_id ON messages WHEN OLD.message_id IS NOT NEW.message_id OR OLD.project_id IS NOT NEW.project_id
                    BEGIN DELETE FROM ledger_relation_audit; END;
CREATE TRIGGER relation_audit_messages_replace
                BEFORE INSERT ON messages
                WHEN EXISTS(SELECT 1 FROM messages
                            WHERE message_id=NEW.message_id
                              AND project_id IS NOT NEW.project_id)
                BEGIN DELETE FROM ledger_relation_audit; END;
CREATE TABLE ledger_relation_recount(singleton INTEGER PRIMARY KEY CHECK(singleton=1), counted_at REAL NOT NULL, marker TEXT NOT NULL);
CREATE TABLE cloud_usage_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1), verified INTEGER NOT NULL CHECK(verified IN (0,1)), mutations INTEGER NOT NULL, audited_at REAL NOT NULL);
CREATE INDEX idx_cloud_usage_reservation ON artifacts(kind, CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END) WHERE kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition');
CREATE TRIGGER cloud_usage_guard_replace BEFORE INSERT ON artifacts WHEN EXISTS(SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')) BEGIN UPDATE cloud_usage_guard SET mutations=mutations+1; END;
CREATE TRIGGER cloud_usage_guard_insert AFTER INSERT ON artifacts WHEN NEW.kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') BEGIN UPDATE cloud_usage_guard SET verified=0 WHERE CASE WHEN typeof(NEW.meta)!='text' OR length(NEW.meta)>32768 OR NOT json_valid(NEW.meta) THEN 1 WHEN json_type(NEW.meta)!='object' THEN 1 WHEN EXISTS (SELECT 1 FROM json_tree(NEW.meta) WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 WHEN EXISTS (SELECT 1 FROM json_tree(NEW.meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 WHEN (SELECT COUNT(*) FROM json_each(NEW.meta) WHERE key='reservation_id' AND type!='null')!=(json_extract(NEW.meta,'$.reservation_id') IS NOT NULL) THEN 1 ELSE 0 END; UPDATE cloud_usage_guard SET mutations=mutations+1 WHERE EXISTS(SELECT 1 FROM artifacts WHERE artifact_id>NEW.artifact_id); END;
CREATE TRIGGER cloud_usage_guard_update AFTER UPDATE ON artifacts WHEN OLD.kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') OR NEW.kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') BEGIN UPDATE cloud_usage_guard SET mutations=mutations+1; UPDATE cloud_usage_guard SET verified=0 WHERE NEW.kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') AND CASE WHEN typeof(NEW.meta)!='text' OR length(NEW.meta)>32768 OR NOT json_valid(NEW.meta) THEN 1 WHEN json_type(NEW.meta)!='object' THEN 1 WHEN EXISTS (SELECT 1 FROM json_tree(NEW.meta) WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 WHEN EXISTS (SELECT 1 FROM json_tree(NEW.meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 WHEN (SELECT COUNT(*) FROM json_each(NEW.meta) WHERE key='reservation_id' AND type!='null')!=(json_extract(NEW.meta,'$.reservation_id') IS NOT NULL) THEN 1 ELSE 0 END; END;
CREATE TRIGGER cloud_usage_guard_delete AFTER DELETE ON artifacts WHEN OLD.kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') BEGIN UPDATE cloud_usage_guard SET mutations=mutations+1; END;
INSERT INTO ledger_relation_guards VALUES('artifacts','enforce',0,0);
INSERT INTO ledger_relation_guards VALUES('attachments','enforce',0,0);
INSERT INTO ledger_relation_audit VALUES('artifacts','enforce');
INSERT INTO ledger_relation_audit VALUES('attachments','enforce');
INSERT INTO ledger_relation_recount VALUES(1,1700000000.0,'0:None:0');
INSERT INTO cloud_usage_guard VALUES(1,1,0,1700000000.0);
