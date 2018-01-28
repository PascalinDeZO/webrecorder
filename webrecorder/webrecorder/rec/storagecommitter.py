import os
import redis
import base64
import json

from warcio.timeutils import sec_to_timestamp, timestamp_now
from webrecorder.models.recording import Recording
from webrecorder.models.base import BaseAccess


# ============================================================================
class StorageCommitter(object):
    DEL_Q = 'q:del:{name}'
    MOVE_Q = 'q:mov:{name}'

    def __init__(self, config):
        super(StorageCommitter, self).__init__()

        self.redis_base_url = os.environ['REDIS_BASE_URL']

        self.redis = redis.StrictRedis.from_url(self.redis_base_url, decode_responses=True)

        self.record_root_dir = os.environ['RECORD_ROOT']

        self.warc_path_templ = config['warc_path_templ']
        self.warc_path_templ = self.record_root_dir + self.warc_path_templ

        self.warc_key_templ = config['warc_key_templ']

        self.commit_wait_templ = config['commit_wait_templ']
        self.commit_wait_secs = int(config['commit_wait_secs'])

        self.index_name_templ = config['index_name_templ']
        self.info_index_key = config['info_index_key']

        self.full_warc_prefix = config['full_warc_prefix']

        self.cdxj_key = config['cdxj_key_templ'].format(user='*', coll='*', rec='*')

        self.default_storage_profile = self.create_default_profile(config)

        self.storage_class_map = {}

        self.storage_key_templ = config['storage_key_templ']

        self.temp_prefix = config['temp_prefix']

        self._init_storage()

        print('Storage Committer Root: ' + self.record_root_dir)

    def _init_storage(self):
        from webrecorder.rec.s3 import S3Storage
        self.add_storage_class('s3', S3Storage)

    def create_default_profile(self, config):
        storage_type = os.environ.get('DEFAULT_STORAGE', 'local')

        profile = {'type': storage_type}

        if storage_type == 's3':
            s3_root = os.environ.get('S3_ROOT')
            s3_root += config['storage_path_templ']

            profile['remote_url_templ'] = s3_root

        return profile

    def commit_file(self, user, collection, dirname, filename, obj_type,
                    update_key, curr_value, update_prop=None):

        if user.is_anon():
            return True

        # not a local filename
        if '://' in curr_value and not curr_value.startswith('local'):
            return True

        full_filename = os.path.join(dirname, filename)

        storage = self.get_storage(user, collection)
        if not storage:
            return True

        commit_wait = self.commit_wait_templ.format(filename=full_filename)

        if self.redis.get(commit_wait) != b'1':
            if not storage.upload_file(user.name, filename, full_filename, obj_type):
                return False

            self.redis.setex(commit_wait, self.commit_wait_secs, 1)

        # already uploaded, see if it is accessible
        # if so, finalize and delete original
        remote_url = storage.get_valid_remote_url(user, filename, obj_type)
        if not remote_url:
            print('Not yet available: {0}'.format(full_filename))
            return False

        update_prop = update_prop or filename
        self.redis.hset(update_key, update_prop, remote_url)

        if not self.delete_committed(full_filename):
            return False

        return True

    def write_cdxj(self, warc_key, dirname, cdxj_key, timestamp):
        cdxj_filename = self.redis.hget(warc_key, self.info_index_key)
        if cdxj_filename:
            return cdxj_filename

        randstr = base64.b32encode(os.urandom(5)).decode('utf-8')

        cdxj_filename = self.index_name_templ.format(timestamp=timestamp,
                                                     random=randstr)

        os.makedirs(dirname, exist_ok=True)

        full_filename = os.path.join(dirname, cdxj_filename)

        cdxj_list = self.redis.zrange(cdxj_key, 0, -1)

        with open(full_filename, 'wt') as fh:
            for cdxj in cdxj_list:
                fh.write(cdxj + '\n')

        full_url = self.full_warc_prefix + full_filename.replace(os.path.sep, '/')

        self.redis.hset(warc_key, self.info_index_key, full_url)

        return cdxj_filename

    def remove_if_empty(self, user_dir):
        # attempt to remove the dir, if empty
        try:
            os.rmdir(user_dir)
            print('Removed dir ' + user_dir)
        except:
            pass

    def process_moves(self, q_name):
        move_q = self.MOVE_Q.format(name=q_name)

        while True:
            data = self.redis.lpop(move_q)
            if not data:
                break

            move = json.loads(data)
            print('MOVE', move)

            try:
                move_from = move['from']
                if os.path.isfile(move_from):
                    self.redis.publish('close_file', move_from)

                    move_to = os.path.join(self.warc_path_templ.format(user=move['to_user']),
                                           move['name'])

                    if move_from != move_to:
                        os.renames(move_from, move_to)
                        self.redis.hset(move['hkey'], move['name'], self.full_warc_prefix + move_to)

            except Exception as e:
                import traceback
                traceback.print_exc()
                print(e)

    def process_deletes(self, q_name):
        del_q = self.DEL_Q.format(name=q_name)

        while True:
            filename = self.redis.lpop(del_q)
            if not filename:
                break

            if os.path.isfile(filename):
                try:
                    self.redis.publish('close_file', filename)
                    #self.recorder.writer.close_file(filename)
                    print('Deleting: ' + filename)
                    os.remove(filename)
                except Exception as e:
                    print(e)
        return

        delete_user = data.get('delete_user')
        if not delete_user:
            return

        user_path = self.warc_path_templ.format(user=delete_user)
        user_path += '*.*'

        for filename in glob.glob(user_path):
            try:
                print('Deleting Local WARC: ' + filename)
                os.remove(filename)

            except Exception as e:
                print(e)



    def __call__(self):
        self.process_deletes('s3')
        self.process_deletes('nginx')

        self.process_moves('local')

        for cdxj_key in self.redis.scan_iter(self.cdxj_key):
            self.process_cdxj_key(cdxj_key)

        self.redis.publish('close_idle', '')

    def process_cdxj_key(self, cdxj_key):
        base_key = cdxj_key.rsplit(':cdxj', 1)[0]
        if self.redis.exists(base_key + ':open'):
            return

        #_, user, coll, rec = base_key.split(':', 3)
        _, rec = base_key.split(':', 1)

        recording = Recording(my_id=rec,
                              redis=self.redis,
                              access=BaseAccess())

        collection = recording.get_owner()
        user = collection.get_owner()

        user_dir = os.path.join(self.record_root_dir, user.my_id)

        warc_key = base_key + ':warc'
        warcs = self.redis.hgetall(warc_key)

        info_key = base_key + ':info'

        self.redis.publish('close_rec', info_key)

        try:
            timestamp = sec_to_timestamp(int(self.redis.hget(info_key, 'updated_at')))
        except:
            timestamp = timestamp_now()

        cdxj_filename = self.write_cdxj(warc_key, user_dir, cdxj_key, timestamp)

        all_done = self.commit_file(user, collection, user_dir,
                                    cdxj_filename, 'indexes', warc_key,
                                    cdxj_filename, self.info_index_key)

        for warc_filename in warcs.keys():
            value = warcs[warc_filename]
            done = self.commit_file(user, collection, user_dir,
                                    warc_filename, 'warcs', warc_key,
                                    value)

            all_done = all_done and done

        if all_done:
            print('Deleting Redis Key: ' + cdxj_key)
            self.redis.delete(cdxj_key)
            self.remove_if_empty(user_dir)

    def delete_committed(self, full_filename):
        print('Commit Verified, Deleting: {0}'.format(full_filename))
        try:
            os.remove(full_filename)
            return True
        except Exception as e:
            print(e)

        return False

    def get_storage(self, user, collection):
        if self.is_temp(user):
            return None

        storage_type = collection.get_prop('storage_type')

        config = None

        # attempt to find storage profile by name
        if storage_type:
            config = self.redis.hgetall(self.storage_key_templ.format(name=storage_type))

        # default storage profile
        if not config:
            config = self.default_storage_profile

        # storage profile class stored in profile 'type'
        storage_class = self.storage_class_map.get(config['type'])

        # keeping local storage only
        if not storage_class:
            return None

        return storage_class(config)

    def add_storage_class(self, type_, cls):
        self.storage_class_map[type_] = cls


# =============================================================================
if __name__ == "__main__":
    from webrecorder.rec.worker import Worker
    Worker(StorageCommitter).run()


