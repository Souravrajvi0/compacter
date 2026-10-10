CREATE TABLE `message` (
          `id` text PRIMARY KEY,
          `session_id` text NOT NULL,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL,
          `data` text NOT NULL,
          CONSTRAINT `fk_message_session_id_session_id_fk` FOREIGN KEY (`session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE
        );
CREATE TABLE `part` (
          `id` text PRIMARY KEY,
          `message_id` text NOT NULL,
          `session_id` text NOT NULL,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL,
          `data` text NOT NULL,
          CONSTRAINT `fk_part_message_id_message_id_fk` FOREIGN KEY (`message_id`) REFERENCES `message`(`id`) ON DELETE CASCADE
        );
CREATE TABLE `session` (
          `id` text PRIMARY KEY,
          `project_id` text NOT NULL,
          `workspace_id` text,
          `parent_id` text,
          `slug` text NOT NULL,
          `directory` text NOT NULL,
          `path` text,
          `title` text NOT NULL,
          `version` text NOT NULL,
          `share_url` text,
          `summary_additions` integer,
          `summary_deletions` integer,
          `summary_files` integer,
          `summary_diffs` text,
          `metadata` text,
          `cost` real DEFAULT 0 NOT NULL,
          `tokens_input` integer DEFAULT 0 NOT NULL,
          `tokens_output` integer DEFAULT 0 NOT NULL,
          `tokens_reasoning` integer DEFAULT 0 NOT NULL,
          `tokens_cache_read` integer DEFAULT 0 NOT NULL,
          `tokens_cache_write` integer DEFAULT 0 NOT NULL,
          `revert` text,
          `permission` text,
          `agent` text,
          `model` text,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL,
          `time_compacting` integer,
          `time_archived` integer,
          CONSTRAINT `fk_session_project_id_project_id_fk` FOREIGN KEY (`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE
        );

insert into session (id,project_id,slug,directory,title,version,cost,tokens_input,tokens_output,tokens_reasoning,tokens_cache_read,tokens_cache_write,time_created,time_updated) values ('ses_1','p1','s','@PROJECT@','t','1.18.35',0,0,0,0,0,0,1,2);
insert into message values ('msg_1','ses_1',1,1,'{"role":"user","agent":"build","mode":"build","modelID":"m","providerID":"p","time":{"created":1760000000000}}');
insert into part values ('prt_1','msg_1','ses_1',1,1,'{"type":"text","text":"fix the auth bug"}');
insert into message values ('msg_2','ses_1',2,2,'{"role":"assistant","agent":"build","mode":"build","modelID":"m","providerID":"p","time":{"created":1760000001000}}');
insert into part values ('prt_2','msg_2','ses_1',2,2,'{"type":"step-start"}');
insert into part values ('prt_3','msg_2','ses_1',3,3,'{"type":"tool","tool":"bash","callID":"call_1","state":{"input":{"command":"ls"},"output":"a.py"}}');
insert into part values ('prt_4','msg_2','ses_1',4,4,'{"type":"text","text":"patched token check"}');
