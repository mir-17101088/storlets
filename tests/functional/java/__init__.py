# Copyright (c) 2010-2016 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from tests.functional import StorletFunctionalTest, PATH_TO_STORLETS

BIN_DIR = 'bin'


class StorletJavaFunctionalTest(StorletFunctionalTest):
    def setUp(self, storlet_dir, storlet_name, storlet_main,
              container, storlet_file, dep_names=None, headers=None):
        storlet_dir = os.path.join('java', storlet_dir)
        path_to_bundle = os.path.join(PATH_TO_STORLETS, storlet_dir,
                                      BIN_DIR)
        super(StorletJavaFunctionalTest, self).setUp('Java',
                                                     path_to_bundle,
                                                     storlet_dir,
                                                     storlet_name,
                                                     storlet_main,
                                                     container,
                                                     storlet_file,
                                                     dep_names,
                                                     headers)
