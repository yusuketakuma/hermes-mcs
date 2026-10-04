-- Entirely invented regression records; never copied from posts or a DB.
-- All columns below exist in every released historical shape in origins.json.
INSERT INTO runs(run_id,started_at,finished_at,snapshot_ts,status,error,kind)
VALUES(1,100,110,123,'ok','','tick');
INSERT INTO patients(project_id,project_type,patient_name,disease,station_name,
  url,fetch_state,fetch_reason,last_complete_fetch,last_seen,created_at,
  coverage_ts,history_target,history_floor,history_page)
VALUES(101,'medical','synthetic patient','synthetic condition','synthetic station',
  'https://invalid.test/projects/101','incomplete','synthetic gap',90,100,50,
  1780000000,1700000000,1690000000,4);
INSERT INTO patients(project_id,patient_name,history_floor,history_page,
  history_target,coverage_ts)
VALUES(102,'synthetic archived patient',-1,0,1600000000,1700000000);
INSERT INTO messages(message_id,project_id,parent_id,sender_id,sender_name,
  sender_type,profession,organization,posted_at,posted_at_ts,is_unread,
  body_html,body_text,body_state,content_hash,reply_count,first_seen,
  updated_seen)
VALUES(201,101,NULL,301,'synthetic sender','user','synthetic role','synthetic org',
  '2026-09-01T00:00:00+00:00',1788220800,1,
  '<p>synthetic upgrade root</p>','synthetic upgrade root','full',
  '24ebdf72270bcf48941cd5ed25aec44532b5aaabb456a14cd14c15339614f38c',
  1,100,110);
INSERT INTO messages(message_id,project_id,parent_id,sender_id,sender_name,
  posted_at,posted_at_ts,is_unread,body_html,body_text,body_state,content_hash,
  reply_count,first_seen,updated_seen)
VALUES(202,101,201,302,'synthetic reply sender',
  '2026-09-01T00:01:00+00:00',1788220860,1,
  '<p>synthetic upgrade reply</p>','synthetic upgrade reply','full',
  '97ab6a50d6ba8408b50043681496a8dd1ee35876faa340b2c75186262b4071c1',
  0,101,112);
INSERT INTO attachments(attachment_id,message_id,file_id,name,url,local_path,
  bytes,sha256,state,downloaded_at,created_at,attempts,next_try,error)
VALUES(401,201,'synthetic-file','synthetic.txt','https://invalid.test/file',
  'attachments/synthetic.txt',26,
  'd207023f51a98ae8f477b44f962c0faf6057193a1caef3d587b05f936e4bf807',
  'downloaded',120,100,2,130,NULL);
INSERT INTO fetch_jobs(job_id,kind,project_id,message_id,parent_id,payload,
  state,attempts,next_try,created_at,updated_at)
VALUES(501,'history',101,0,NULL,
  '{"since":1700000000,"page":4,"synthetic":true}', 'pending',2,200,100,130);
INSERT INTO fetch_jobs(job_id,kind,project_id,message_id,parent_id,payload,
  state,attempts,next_try,created_at,updated_at)
VALUES(502,'reply',101,202,201,'{"synthetic":true}','failed',3,NULL,100,130);
INSERT INTO artifacts(artifact_id,kind,project_id,message_id,content,model,meta,
  created_at)
VALUES(601,'synthetic_derived',101,201,'{"synthetic":true}',
  'synthetic-model','{"hash":"synthetic-artifact-hash"}',140);
INSERT INTO requests(request_id,project_id,source_message_id,source_hash,title,
  assignee,due_date,status,revision,created_at,updated_at)
VALUES(701,101,201,
  '24ebdf72270bcf48941cd5ed25aec44532b5aaabb456a14cd14c15339614f38c',
  'synthetic confirmed request','synthetic assignee','2026-10-01',
  'in_progress',2,140,150);
INSERT INTO command_receipts(command_id,payload_hash,project_id,request_id,
  outcome,receipt_json,processed_at)
VALUES('synthetic-applied','synthetic-payload-hash',101,701,'applied',
  '{"cmd":"request.create","synthetic":true}',150);
INSERT INTO command_receipts(command_id,payload_hash,project_id,request_id,
  outcome,receipt_json,processed_at)
VALUES('synthetic-rejected','synthetic-rejected-hash',101,701,'rejected',
  '{"synthetic":true,"error":"synthetic_denied"}',151);
INSERT INTO read_marks(project_id,snapshot_ts,marked_at,status)
VALUES(101,123,160,'unknown');
INSERT INTO notify_outbox(event_id,kind,project_id,payload,state,attempts,
  next_try,accepted_ref,created_at,updated_at,progress)
VALUES(801,'new_messages',101,'{"message_ids":[201],"synthetic":true}',
  'accepted',1,NULL,'synthetic-remote-ref',100,120,'{"next":1,"sending":1}');
INSERT INTO notify_outbox(event_id,kind,project_id,payload,state,attempts,
  next_try,created_at,updated_at,progress)
VALUES(802,'new_messages',101,'{"message_ids":[202],"synthetic":true}',
  'pending',2,200,101,121,'{"next":0,"sending":0}');
INSERT INTO snapshot_meta(singleton,generation_id,generated_at)
VALUES(1,'synthetic-historical-generation',160);
