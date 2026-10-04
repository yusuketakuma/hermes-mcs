-- Real schema-8 fields, absent from released schema-7 shapes.
INSERT INTO message_metadata(message_id,source,content,checked_at,last_error)
VALUES(201,'capture','{"synthetic":true,"reactions":[]}',130,NULL);
INSERT INTO message_reaction_actors(message_id,actor_id,reaction_type,profession,
  observed_at)
VALUES(201,301,'synthetic-reaction','synthetic role',130);
INSERT INTO message_reaction_actor_fetch(message_id,complete_at,checked_at,last_error)
VALUES(201,130,130,NULL);
