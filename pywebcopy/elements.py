# -*- coding: utf-8 -*-

"""
pywebcopy.elements
~~~~~~~~~~~~~~~~~~

Asset elements of a web page.

"""

import os.path
from os import cpu_count
from io import BytesIO
from typing import IO
from mimetypes import guess_all_extensions
from shutil import copyfileobj
from threading import Thread, Semaphore

from . import LOGGER
from .configs import config
from .compat import bytes, pathname2url
from .core import get, _watermark, is_allowed
from .globals import CSS_FILES_RE, CSS_IMPORTS_RE, CSS_URLS_RE
from .urls import URLTransformer, relate


__all__ = ['FileMixin', 'TagBase', 'AnchorTag', 'ImgTag', 'ScriptTag', 'LinkTag']


# This makes sure the number of thread working remains
# under control so that the resource overloading could
# be prevented and the program remains memory efficient
#: new in version: 6.0.0
threads_manager = Semaphore((cpu_count() or 1) * 2)


class FileMixin(URLTransformer, Thread):
    """Wrapper for every Asset type which is used by a web page.
     e.g. css, js, img, link.

     It inherits the URLTransformer() object to provide file path manipulations.

     :type url: str
     :type base_url: str
     :param str url: a url type string for working
     :param optional str base_url: base url of the website i.e. domain name
     :param optional str base_path: base path where the files
        will be stored after download
     """
    rel_path = None     # Initialiser for a dummy use case

    def __init__(self, url, base_url=None, base_path=None):
        URLTransformer.__init__(self, url, base_url, base_path)
        Thread.__init__(self)

    def __repr__(self):
        return '<File(%s)>' % self.url

    def run(self):
        # XXX: This could wait for any condition
        with threads_manager:
            self.download_file()

    save_file = run

    def download_file(self):
        """Retreives the file from the internet.
        Its a minimal and verbose version of the function
        present the core module.

        *Note*: This needs `url` and `file_path` attributes to be present.
        """
        file_path = self.file_path
        file_ext = os.path.splitext(file_path)[1]
        url = self.url

        assert file_path, "Download location needed to be specified!"
        assert isinstance(file_path, str), "Download location must be a string!"
        assert isinstance(url, str), "File url must be a string!"

        if os.path.exists(file_path):
            if not config['over_write']:
                LOGGER.info("File already exists at location: %r" % file_path)
                return
        else:
            #: Make the directories
            try:
                os.makedirs(os.path.dirname(file_path))
            except FileExistsError:
                pass

        req = get(url, stream=True)
        req.raw.decode_content = True

        if req is None or not req.ok:
            LOGGER.error('Failed to load the content of file %s '
                         'from %s' % (file_path, url))
            return

        #: A dynamic file type check is required to take in context of
        #: server generated files like e.g. images. These types of file urls
        #: doesn't necessarily include a file extension. Thus a dynamic guess
        #: list is prepared a all the guesses are checked if they hold true

        #: First check if the extension present in the url is allowed or not
        if not is_allowed(file_ext):
            mime_type = req.headers.get('content-type', '').split(';', 1)[0]

            #: Prepare a guess list of extensions
            file_exts = guess_all_extensions(mime_type, strict=False) or []

            #: Do add the defaults if present
            if self.default_fileext:
                file_exts.extend(['.' + self.default_fileext])

            # now check again
            for ext in file_exts:
                if is_allowed(ext):
                    file_ext = ext
                    break
            else:
                LOGGER.error("File of type %r at url %r is not allowed "
                             "to be downloaded!" % (file_ext, url))
                return

        try:
            # case the function will catch it and log it then return None
            LOGGER.info("Writing file at location %s" % file_path)
            with open(file_path, 'wb') as f:
                #: Actual downloading
                copyfileobj(req.raw, f)
                f.write(_watermark(url))
        except OSError:
            LOGGER.critical("Download failed for the file of "
                            "type %s to location %s" % (file_ext, file_path))
        except Exception as e:
            LOGGER.critical(e)
        else:
            LOGGER.success('File of type %s written successfully '
                           'to %s' % (file_ext, file_path))

    def write_file(self, file_like_object):
        """
        Same as download file but this instead of downloading the
        content it requires you to supply the content as a file like object.

        Parameters
        ----------
        file_like_object: IO | BytesIO
            Contents of the file to be written to disk

        Returns
        -------
            Download location

        """
        file_path = self.file_path
        file_ext = os.path.splitext(file_path)[1]
        url = self.url

        assert hasattr(file_like_object, 'read'), "A file like object with read method is required!"
        assert file_path, "Download location needed to be specified!"
        assert isinstance(file_path, str), "Download location must be a string!"
        assert isinstance(url, str), "File url must be a string!"

        if os.path.exists(file_path):
            if not config['over_write']:
                LOGGER.info("File already exists at location: %r" % file_path)
                return
        else:
            #: Make the directories
            try:
                os.makedirs(os.path.dirname(file_path))
            except FileExistsError:
                pass

        if not is_allowed(file_ext):
            LOGGER.error("File of type %r at url %r is not allowed to be "
                         "downloaded!" % (file_ext, url))
            return

        try:
            # case the function will catch it and log it then return None
            LOGGER.info("Writing file at location %s" % file_path)
            with open(file_path, 'wb') as f:
                #: Actual downloading
                copyfileobj(file_like_object, f)
                f.write(_watermark(url))
        except OSError:
            LOGGER.exception("Download failed for the file of type %s to "
                             "location %s" % (file_ext, file_path), exc_info=True)
        except Exception as e:
            LOGGER.critical(e)
        else:
            LOGGER.success('File of type %s written successfully to %s' % (file_ext, file_path))


class TagBase(FileMixin):
    """Base class for all tag handlers"""

    tag = None


class LinkTag(TagBase):
    """Link tags are special since they can contain either favicons
    or css files and within these css files can be links to other
    css files. Thus these are handled differently from other
    files.

    """
    def __init__(self, *args, **kwargs):
        super(LinkTag, self).__init__(*args, **kwargs)
        self._stack = set()       # sub-files

    def repl(self, match_obj):
        """Processes an url and returns a suited replaceable string.

        :type match_obj: re.MatchObject
        :param match_obj: regex match object to be processed
        :rtype: str
        :return: processed url
        """
        url = match_obj.group(1)

        # url can be base64 encoded content which is not required to be stored
        if url[:4] == b'data':
            return url

        # a path is generated by the cssAsset object and tried to store the file
        # but file could be corrupted to open or write
        # NOTE: self.base_path property needs to be set in order to work properly
        if self.base_path:
            base_path = self.base_path
        else:
            base_path = config['project_folder']

        # decode the url
        str_url = url.decode()

        # If the url is also a css file then it that file also
        # needs to be scanned for urls.
        if str_url.endswith('.css'):    # if the url is of proper style sheet
            new_element = LinkTag(str_url, self.url, base_path)

        else:
            new_element = TagBase(str_url, self.url, base_path)

        # Keep the element in stack
        self._stack.add(new_element)

        # generate a relative path for this downloaded file
        url = pathname2url(relate(new_element.file_path, self.file_path))

        return "url({})".format(url).encode()

    @staticmethod
    def replace_urls(css_string, repl):
        """Extracts url() links and @imports in css.

        All the linked files will be saved and file path
        would be replaced accordingly
        """
        if hasattr(css_string, 'read'):
            css_string = css_string.read()

        assert isinstance(css_string, bytes), "Provide string type contents."
        assert callable(repl), "Repl must be callable type which returns binary strings."

        # the regex matches all those with double mix-match
        # quotes and normal ones
        contents = CSS_URLS_RE.sub(repl, css_string)
        contents = CSS_IMPORTS_RE.sub(repl, contents)

        # return the rewritten bytes contents
        return contents

    def run(self):
        """
        Css files are saved differently because they could have files linked through
        css rules in them which also needs to be downloaded separately.
        Thus css file content needs to be searched for urls and then it will proceed
        as usual.
        """

        # LinkTags can also be specified for elements like favicon etc. Thus a check is necessary
        # to validate it is a proper css file or not.
        if not self.file_name.endswith('.css'):
            super(LinkTag, self).run()

        # Custom request object creation
        req = get(self.url, stream=True)

        # if some error occurs
        if not req or not req.ok:
            LOGGER.error("URL returned an unknown response: %s" % self.url)
            return

        # Try to avoid pulling the contents in the ram
        # while substuting urls in the contents would NOT
        # work as expected because the regex won't match
        # correctly, thus we have to load the whole file
        # in at once. But will try to minimise the footprint
        # Extracts urls from `url()` and `@imports` rules in the css file.
        # the regex matches all those with double mix-match quotes and normal ones
        # all the linked files will be saved and file paths would be replaced accordingly
        contents = BytesIO(CSS_FILES_RE.sub(self.repl, req.content))

        # log amount of links found
        LOGGER.info('%d CSS linked files are found in file %s'
                    % (len(self._stack), self.file_path))

        # Save the content
        self.write_file(contents)

        # Also invoke the files stored in sub-files stack
        for f in self._stack:
            f.run()


class AnchorTag(TagBase):
    """
    Anchor tag contains links to differents pages or even different websites.
    Thus they doesn't need to saved by default but this class can be overridin to
    provide custom support for anchor tag links.
    """

    def __init__(self, *args, **kwargs):
        super(AnchorTag, self).__init__(*args, **kwargs)

        self.default_fileext = 'html'
        self.check_fileext = True

    """Break the saving action of a html page."""

    def download_file(self):
        return

    def write_file(self, file_like_object):
        return

    def run(self):
        return


class ScriptTag(TagBase):
    """Customises the TagBase() object for js file type."""
    def __init__(self, *args, **kwargs):
        super(ScriptTag, self).__init__(*args, **kwargs)
        self.default_fileext = 'js'


class ImgTag(TagBase):
    """Customises the TagBase() object for images file type."""
    def __init__(self, *args, **kwargs):

        super(ImgTag, self).__init__(*args, **kwargs)
        self.default_fileext = 'jpg'
