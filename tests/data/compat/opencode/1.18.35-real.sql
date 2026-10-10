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
insert into session (id,project_id,workspace_id,parent_id,slug,directory,path,title,version,share_url,summary_additions,summary_deletions,summary_files,summary_diffs,metadata,cost,tokens_input,tokens_output,tokens_reasoning,tokens_cache_read,tokens_cache_write,revert,permission,agent,model,time_created,time_updated,time_compacting,time_archived) values ('ses_edaea6ab4ffeKU60M6kKSzypie','global',NULL,NULL,'cosmic-wizard','@PROJECT@','Users/user/AppData/Local/Temp/claude/C--Users-user/ec49daf0-6a02-41ef-a564-0780c7cab054/scratchpad/real/oc','New session - 2026-10-10T09:12:13.900Z','1.18.35',NULL,0,0,0,NULL,NULL,0.0,12710,31,145,16320,0,NULL,'[{"permission":"question","pattern":"*","action":"deny"},{"permission":"plan_enter","pattern":"*","action":"deny"},{"permission":"plan_exit","pattern":"*","action":"deny"}]','build','{"id":"ling-3.1-flash-free","providerID":"opencode","variant":"default"}',1791623533900,1791623581235,NULL,NULL);
insert into message (id,session_id,time_created,time_updated,data) values ('msg_1251598dd001qUGDJsxydnRD3d','ses_edaea6ab4ffeKU60M6kKSzypie',1791623534813,1791623578236,'{"role":"user","time":{"created":1791623534813},"agent":"build","model":{"providerID":"opencode","modelID":"ling-3.1-flash-free"},"summary":{"diffs":[]}}');
insert into message (id,session_id,time_created,time_updated,data) values ('msg_12515990b001QigLFJKGB3teRn','ses_edaea6ab4ffeKU60M6kKSzypie',1791623534859,1791623578200,'{"parentID":"msg_1251598dd001qUGDJsxydnRD3d","role":"assistant","mode":"build","agent":"build","path":{"cwd":"@HOME@\\AppData\\Local\\Temp\\claude\\C--Users-user\\ec49daf0-6a02-41ef-a564-0780c7cab054\\scratchpad\\real\\oc","root":"/"},"cost":0,"tokens":{"total":14526,"input":12626,"output":15,"reasoning":29,"cache":{"write":0,"read":1856}},"modelID":"ling-3.1-flash-free","providerID":"opencode","time":{"created":1791623534859,"completed":1791623578199},"finish":"tool-calls"}');
insert into message (id,session_id,time_created,time_updated,data) values ('msg_12516425d001lNtOmyu8ovzcuM','ses_edaea6ab4ffeKU60M6kKSzypie',1791623578205,1791623581211,'{"parentID":"msg_1251598dd001qUGDJsxydnRD3d","role":"assistant","mode":"build","agent":"build","path":{"cwd":"@HOME@\\AppData\\Local\\Temp\\claude\\C--Users-user\\ec49daf0-6a02-41ef-a564-0780c7cab054\\scratchpad\\real\\oc","root":"/"},"cost":0,"tokens":{"total":14680,"input":84,"output":16,"reasoning":116,"cache":{"write":0,"read":14464}},"modelID":"ling-3.1-flash-free","providerID":"opencode","time":{"created":1791623578205,"completed":1791623581210},"finish":"stop"}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_1251598ef001ioXLfmp1PEuzgZ','msg_1251598dd001qUGDJsxydnRD3d','ses_edaea6ab4ffeKU60M6kKSzypie',1791623534837,1791623534839,'{"type":"text","text":"\"Run ls with your bash tool, then say in one sentence what notes.txt likely is.\""}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_12516406b001iWvmkkeJDnNcdF','msg_12515990b001QigLFJKGB3teRn','ses_edaea6ab4ffeKU60M6kKSzypie',1791623577707,1791623577709,'{"type":"step-start"}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_125164070001F2PyntesLRNgP3','msg_12515990b001QigLFJKGB3teRn','ses_edaea6ab4ffeKU60M6kKSzypie',1791623577712,1791623577820,'{"type":"reasoning","text":"The user wants me to run `ls` with my bash tool, then say in one sentence what notes.txt likely is. Let me do that.","time":{"start":1791623577712,"end":1791623577818}}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_1251640df001LsO5h1nOu9JdaM','msg_12515990b001QigLFJKGB3teRn','ses_edaea6ab4ffeKU60M6kKSzypie',1791623577823,1791623578183,'{"type":"tool","tool":"bash","callID":"call_3902ee6625514db6b67983a8","state":{"status":"completed","input":{"command":"ls"},"output":"notes.txt\n","metadata":{"output":"notes.txt\n","exit":0,"truncated":false},"title":"ls","time":{"start":1791623577925,"end":1791623578180}}}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_12516424e0012jphMXSD34rDGu','msg_12515990b001QigLFJKGB3teRn','ses_edaea6ab4ffeKU60M6kKSzypie',1791623578190,1791623578191,'{"reason":"tool-calls","type":"step-finish","tokens":{"total":14526,"input":12626,"output":15,"reasoning":29,"cache":{"write":0,"read":1856}},"cost":0}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_1251649260012azA2RfCZIx5GN','msg_12516425d001lNtOmyu8ovzcuM','ses_edaea6ab4ffeKU60M6kKSzypie',1791623579943,1791623579945,'{"type":"step-start"}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_12516492a001YO6tbGBV1rJB4U','msg_12516425d001lNtOmyu8ovzcuM','ses_edaea6ab4ffeKU60M6kKSzypie',1791623579947,1791623580912,'{"type":"reasoning","text":"The ls output shows only \"notes.txt\" exists in the current directory. So notes.txt is likely the only file in this working directory — probably a scratch/working file containing notes for this task. I should say in one sentence what it likely is. I could read it to be more accurate, but the user only asked for ls and a one-sentence inference. Let me just say it''s likely the sole file in this scratchpad directory, probably a notes file for the current task.","time":{"start":1791623579946,"end":1791623580909}}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_125164cf5001OkdKudqChL6xBs','msg_12516425d001lNtOmyu8ovzcuM','ses_edaea6ab4ffeKU60M6kKSzypie',1791623580917,1791623581196,'{"type":"text","text":"The directory contains only `notes.txt`, so it''s likely the single working/scratch file in this session, probably holding notes for the task at hand.","time":{"start":1791623580917,"end":1791623581194}}');
insert into part (id,message_id,session_id,time_created,time_updated,data) values ('prt_125164e10001hYcovKkQnxLV3c','msg_12516425d001lNtOmyu8ovzcuM','ses_edaea6ab4ffeKU60M6kKSzypie',1791623581200,1791623581202,'{"reason":"stop","type":"step-finish","tokens":{"total":14680,"input":84,"output":16,"reasoning":116,"cache":{"write":0,"read":14464}},"cost":0}');
