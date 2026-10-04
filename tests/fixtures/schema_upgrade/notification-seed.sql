-- Only loaded when the historical shape really contains notification tables.
INSERT INTO notification_cards(card_id,card_key,kind,project_id,root_message_id,
  anchor_key,source_fp,content_fp,delivery_state,created_at,updated_at)
VALUES(901,'synthetic-card','thread',101,201,'synthetic-anchor',
  'synthetic-source-fp','synthetic-content-fp','delivery_unknown',100,130);
INSERT INTO notification_view_manifests(manifest_id,card_id,render_rev,
  source_generation,presentation_generation,shown,created_at)
VALUES(902,901,1,1,1,'{"synthetic":true}',120);
INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,manifest_id,
  route_epoch,payload_hash,correlation,state,created_at,updated_at)
VALUES('synthetic-delivery',901,'create',1,902,1,
  'synthetic-render-hash','synthetic-correlation','held',120,130);
INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,
  begin_command_id,state,created_at,finished_at)
VALUES('synthetic-attempt','synthetic-delivery','synthetic-begin','unknown',120,130);
INSERT INTO notification_restore_holds(hold_id,card_id,delivery_id,attempt_id,
  events_json,reason,scope_json,held_at)
VALUES(903,901,'synthetic-delivery','synthetic-attempt','[801]',
  'synthetic-historical-hold','{"synthetic":true}',130);
INSERT INTO notification_intent_batches(event_id,frozen_payload,payload_hash,
  route_epoch,sealed_at)
VALUES(801,'{"message_ids":[201]}','synthetic-batch-hash',1,120);
INSERT INTO notification_intent_cards(event_id,card_id,coverage,
  required_render_rev,delivery_id,state)
VALUES(801,901,'[201]',1,'synthetic-delivery','pending');
INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,payload_sha256,
  bytes,name,attachment_id,state,attempt_id,updated_at)
VALUES('synthetic-delivery','synthetic-part','attachment_part',0,
  'synthetic-part-hash',26,'synthetic.txt',401,'held','synthetic-part-attempt',130);
INSERT INTO notification_acknowledgements(ack_id,card_id,manifest_id,actor,
  command_id,receipt_ref,created_at)
VALUES(904,901,902,'synthetic actor','synthetic-ack','synthetic-ack-receipt',130);
INSERT INTO notification_action_tokens(token,card_id,action,params,expires_at,
  created_at)
VALUES('synthetic-token',901,'synthetic-action','{"synthetic":true}',200,120);
INSERT INTO notification_triage(card_id,owner,state,revision,updated_at)
VALUES(901,'synthetic owner','assigned',2,130);
INSERT INTO notification_meta(singleton,notify_dirty) VALUES(1,1);
